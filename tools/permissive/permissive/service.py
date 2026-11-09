"""Authenticated asynchronous task API shared by desktop, CLI, and MCP clients."""

import asyncio
import json
import sqlite3
from pathlib import Path, PureWindowsPath
import time

from aiohttp import web
from .store import Store, TERMINAL
from .auth import Authenticator
from cryptography.exceptions import InvalidSignature
from .browser_bridge import BrowserBridge
from .workspaces import directory_key, normalize_guest_directory, skill_catalog, skill_document
from .skill_library import category_name, export_bundle, import_bundle
from .browser_settings import browser_settings, browser_catalog, open_installed_profile, setup_installed_profile
from .chat_settings import CHAT_FIELDS, chat_settings
from .privacy import http_server_options
from .attachments import Attachments
from .temporary_credentials import TemporaryCredentials, identifier, validate_envelope


async def library_body(request, fields):
    body = await request.json()
    optional = {'segmentId'} if {'taskId', 'messageId', 'submissionId'} <= fields else set()
    if not isinstance(body, dict) or set(body) - optional != fields:
        raise ValueError("Expected " + ", ".join(sorted(fields)))
    if 'segmentId' in body and (not isinstance(body['segmentId'], str) or not body['segmentId']):
        raise ValueError('Select a valid chat segment')
    return body


class Service:
    def __init__(self, home, runtime):
        self.auth = Authenticator(home)
        self.store, self.runtime = Store(home, guest_paths=getattr(runtime, 'guest_paths', False)), runtime
        if hasattr(runtime, 'attach'):
            runtime.attach(self.store)
        self.attachments = Attachments(self.store)
        from .mobile_replay import MobileReplay
        self.mobile_replay = MobileReplay()
        from .history import operation_key
        from .secure_delete import defer_overwrites, overwrite_file
        for manifest in (self.store.home / 'history-edits').glob('*.json'):
            try:
                pending = json.loads(manifest.read_text(encoding='utf-8'))
                replacement = pending['replacement']
                row = self.store.db.execute('SELECT conversation_id FROM tasks WHERE id=?', (replacement,)).fetchone()
                if row and row[0] == replacement:
                    with self.store.db:
                        self.store.db.execute('UPDATE tasks SET idempotency_key=? WHERE id=?', (operation_key(manifest.stem, pending['body'], True), replacement))
                        self.store.db.execute('INSERT OR IGNORE INTO history_deletions VALUES (?,?,?)', (manifest.stem, replacement, int(bool(pending.get('guestErased')))))
                        self.store.db.execute('INSERT OR REPLACE INTO conversation_queues VALUES (?,1)', (manifest.stem,))
                elif not row or row[0] != manifest.stem:
                    continue
                with defer_overwrites():
                    overwrite_file(manifest, self.store.home)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                pass  # Keep the journal and explicit retry path on failure.
        self.pending_history = any((self.store.home / 'history-edits').glob('*.json'))
        self.jobs, self.thread_owners = {}, {}
        self.cancellations = {}
        self.control_locks = {}
        self.delivery_locks = {}
        self.steering_operations = {}
        self.history_maintenance = False
        self.closing = False
        for task in self.store.db.execute("SELECT id,thread_id FROM tasks WHERE status='paused' AND thread_id IS NOT NULL").fetchall():
            self.thread_owners[task["thread_id"]] = task["id"]
        self.slots = asyncio.Semaphore(getattr(runtime, 'max_concurrent_tasks', 2))
        self.browser = BrowserBridge()
        if hasattr(runtime, 'capability_probe'):
            runtime.capability_probe = self.browser.capabilities
        self.stopped = asyncio.Event()
        self.browser_setup_lock = asyncio.Lock()
        self.credentials = TemporaryCredentials()
        self.credential_reaper = None
        self.history_reaper = None
        self.overwrite_job = None
        self.history_wakeup = asyncio.Event()
        self.history_operations = {}
        self.history_sources = set()
        self.history_copy_lock = asyncio.Lock()
        self.segment_operations = {}
        self.segment_reaper = None
        self.segment_migration_lock = asyncio.Lock()
        self.regeneration_finalizers = {}

    async def ensure_segments(self, ident):
        from .reply_versions import pending
        if pending(self.store, ident):
            publication = self.segment_operations.get(ident)
            if publication:
                await asyncio.shield(publication)
            return
        if self.history_maintenance:
            return
        if self.store.segments.ready(ident):
            info = self.store.segments.info(ident)
            if not self.store.segments.needs_recovery(info):
                return
        # Running/paused native workers retain their profile until a safe idle
        # boundary. Reading an unmigrated active chat remains supported.
        if self.store.db.execute("SELECT 1 FROM tasks WHERE conversation_id=? AND status NOT IN ('completed','cancelled','failed','interrupted')", (ident,)).fetchone():
            return
        async def migrate():
            async with self.segment_migration_lock:
                def work():
                    store = Store.worker_connection(self.store.home, self.store.guest_paths)
                    try:
                        if store.segments.ready(ident):
                            store.segments.recover(ident, getattr(self.runtime, 'summarize_history', None))
                        else:
                            store.segments.migrate(ident, getattr(self.runtime, 'summarize_history', None))
                    finally:
                        store.close()
                await asyncio.to_thread(work)
        job = self.segment_operations.get(ident)
        if job is None:
            job = asyncio.create_task(migrate())
            self.segment_operations[ident] = job
            job.add_done_callback(lambda _: self.segment_operations.pop(ident, None))
        await asyncio.shield(job)

    async def segment_maintenance(self, app):
        async def migrate_saved():
            await asyncio.sleep(1)
            while not self.closing:
                rows = self.store.db.execute('SELECT DISTINCT conversation_id FROM tasks WHERE conversation_id NOT IN (SELECT source_id FROM history_deletions)').fetchall()
                for row in rows:
                    try:
                        await self.ensure_segments(row[0])
                    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error):
                        pass  # Legacy data stays authoritative until validation succeeds.
                    await asyncio.sleep(.1)
                await asyncio.sleep(30)
        self.segment_reaper = asyncio.create_task(migrate_saved())

    @web.middleware
    async def authenticate(self, request, handler):
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Browser origins are not API clients")
        try:
            request["authenticatedKeyId"] = self.auth.verify(request.method, request.raw_path, await request.read(), request.headers)
        except (ValueError, KeyError, TypeError, OSError, InvalidSignature):
            raise web.HTTPUnauthorized(text="A valid public-key request signature is required") from None
        try:
            target = request.match_info.get('id')
            if target:
                owner = self.store.db.execute('SELECT conversation_id FROM tasks WHERE id=?', (target,)).fetchone()
                publication = self.segment_operations.get(owner[0] if owner else target)
                if publication:
                    await asyncio.shield(publication)
            if request.method not in {'GET', 'HEAD'}:
                target = request.match_info.get('id')
                if request.path == '/v1/tasks':
                    submitted = await request.json()
                    target = submitted.get('resumeTaskId') if isinstance(submitted, dict) else None
                if target:
                    row = self.store.db.execute('SELECT conversation_id FROM tasks WHERE id=?', (target,)).fetchone()
                    job = self.segment_operations.get(row[0] if row else target)
                    if job:
                        await asyncio.shield(job)
            if self.history_maintenance and request.method not in {'GET', 'HEAD'}:
                raise web.HTTPConflict(text='Chat history maintenance is in progress; retry shortly')
            if request.method not in {'GET', 'HEAD'}:
                from .reply_versions import pending
                target = request.match_info.get('id')
                if request.path == '/v1/tasks':
                    submitted = await request.json()
                    target = submitted.get('resumeTaskId') if isinstance(submitted, dict) else None
                row = self.store.db.execute('SELECT conversation_id FROM tasks WHERE id=?', (target,)).fetchone() if target else None
                chat = row[0] if row else target
                active_branch = self.store.db.execute("SELECT 1 FROM regeneration_jobs WHERE branch_id=? AND phase!='ready'", (chat,)).fetchone() if chat else None
                task_control = active_branch and (request.path.endswith(('/pause', '/resume', '/steer')) or '/followups' in request.path)
                if target and pending(self.store, chat) and not task_control and not request.path.endswith(('/regenerate', '/cancel')):
                    raise web.HTTPConflict(text='Response branch preparation is in progress; retry shortly')
            if self.pending_history and request.method not in {'GET', 'HEAD'} and not request.path.endswith(('/delete-from', '/delete-message', '/regenerate')):
                raise web.HTTPConflict(text='A chat history deletion needs cleanup. Retry Delete from here before making changes.')
            # Superseded chats remain private while their physical cleanup is
            # pending. Only a retry of the original operation may resolve them.
            ident = request.match_info.get('id')
            if ident and request.method not in {'GET', 'HEAD'} and not request.path.endswith(('/fork', '/delete-from', '/delete-message', '/regenerate')):
                source = self.store.db.execute('SELECT conversation_id FROM tasks WHERE id=?', (ident,)).fetchone()
                if ident in self.history_sources or source and source[0] in self.history_sources:
                    raise web.HTTPConflict(text='This chat is being updated. Open the replacement when it is ready.')
            if ident and not request.path.endswith(('/delete-from', '/delete-message', '/regenerate', '/edit-message')):
                retired = self.store.db.execute('SELECT 1 FROM history_deletions WHERE source_id=? OR source_id=(SELECT conversation_id FROM tasks WHERE id=?)', (ident, ident)).fetchone()
                if not retired and '/attachments/' in request.path:
                    retired = self.store.db.execute('SELECT 1 FROM attachments a JOIN tasks t ON t.id=a.owner_task_id JOIN history_deletions d ON d.source_id=t.conversation_id WHERE a.id=?', (ident,)).fetchone()
                if retired and ('/attachments/' in request.path or request.path.startswith('/v1/tasks/')):
                    excluding = [row[0] for row in self.store.db.execute('SELECT source_id FROM history_deletions')]
                    shared = self.store.segments.shared_attachment(ident, excluding) if '/attachments/' in request.path else self.store.segments.shared_task(ident, excluding)
                    if shared:
                        retired = None
                if retired:
                    raise web.HTTPGone(text='This chat has been replaced. Open the updated chat from the chat list.')
            return await handler(request)
        except KeyError:
            raise web.HTTPNotFound(text="Task or artifact not found")
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise web.HTTPBadRequest(text=str(error))

    async def health(self, request):
        return web.json_response(
            {
                "ok": True,
                "model": "/models/deepseek-v4-flash",
                "maxConcurrentModelRequests": getattr(self.runtime, 'max_concurrent_tasks', 2),
                "activeTasks": len(self.jobs),
                "executionRole": getattr(self.runtime, 'role', 'guest'),
                "pendingHistoryDeletions": self.store.db.execute('SELECT count(*) FROM history_deletions').fetchone()[0],
                **({'vmId': self.runtime.binding['vmId'],
                    'environmentId': self.runtime.state['environmentId']}
                   if getattr(self.runtime, 'role', None) == 'controller' else {}),
            }
        )

    async def secure_delete(self, request):
        from .secure_delete import erase_saved_data, pending_path
        body = await library_body(request, {'confirm', 'passes'})
        ident = request.match_info['id']
        if body['confirm'] != ident or type(body['passes']) is not int or body['passes'] != 7:
            raise ValueError('Confirm the selected ID and seven overwrite passes')
        if self.jobs or self.steering_operations or self.history_operations or self.regeneration_finalizers:
            raise web.HTTPConflict(text='Finish or cancel running tasks before secure deletion')
        workspace_id = ident if '/workspaces/' in request.path else None
        retry = pending_path(self.store.home, ident, bool(workspace_id)).exists()
        if workspace_id:
            if not retry:
                self.store.workspace(ident)
            conversations = [row[0] for row in self.store.db.execute('SELECT DISTINCT conversation_id FROM tasks WHERE workspace_id=?', (ident,))]
        else:
            if not retry:
                self.store.conversation_tasks(ident)
            from .reply_versions import members
            conversations = members(self.store, ident)
        from .reply_versions import pending
        if any(pending(self.store, chat) for chat in conversations):
            raise web.HTTPConflict(text='Response branch preparation is in progress; retry shortly')
        self.history_maintenance = True
        try:
            await asyncio.gather(*list(self.segment_operations.values()), return_exceptions=True)
            if workspace_id:
                if getattr(self.runtime, 'role', None) == 'controller':
                    await self.runtime.transport.request('DELETE', '/v1/credentials?workspaceId=' + identifier(workspace_id))
                self.credentials.clear_workspace(workspace_id)
            if hasattr(self.runtime, 'erase_history'):
                for conversation in conversations:
                    await self.runtime.erase_history(conversation, self.store)
            result = erase_saved_data(self.store, conversations, workspace_id)
        except (OSError, sqlite3.Error, RuntimeError):
            raise web.HTTPConflict(text='Secure deletion could not finish. Some data may already be removed; retry when storage is available.') from None
        finally:
            self.history_maintenance = False
            self.mobile_replay.clear()
        return web.json_response(result)

    async def openapi(self, request):
        return web.FileResponse(Path(__file__).parents[1] / "openapi.json")

    async def edit_history(self, request):
        from .history import copy_prefix, edit_path, finish_delete, save_edit, operation_key, message_deletion, all_events
        ident = request.match_info['id']
        operation = request.path.rsplit('/', 1)[-1]
        if operation == 'regenerate':
            return await self.regenerate(request)
        deleting = operation != 'fork'
        required = {'taskId', 'messageId', 'submissionId'} | ({'confirm'} if deleting else set())
        body = await library_body(request, required)
        if operation in {'delete-message', 'regenerate'}:
            body['_operation'] = operation
        self.validate_submission_id(body['submissionId'])
        if not all(isinstance(body[key], str) and body[key] for key in ('taskId', 'messageId')):
            raise ValueError('Select a task and message in this chat')
        if deleting and body['confirm'] != ident:
            raise ValueError('Confirm the selected chat ID')
        key = operation_key(ident, body, deleting)
        if key in self.history_operations:
            replacement = await asyncio.shield(self.history_operations[key])
            return web.json_response(self.store.conversation(replacement))
        existing = self.store.db.execute('SELECT conversation_id FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
        if existing:
            if operation == 'regenerate':
                self.start_regeneration(existing[0])
            return web.json_response(self.store.conversation(existing[0]))
        if self.store.db.execute('SELECT 1 FROM history_deletions WHERE source_id=?', (ident,)).fetchone():
            raise web.HTTPConflict(text='This chat has already been replaced. Open the updated chat from the chat list.')
        if not deleting:
            fork = await self.copy_history(ident, body, operation, key)
            return web.json_response(self.store.conversation(fork))
        manifest = edit_path(self.store, ident)
        if self.pending_history and not manifest.exists():
            raise web.HTTPConflict(text='Retry the unfinished chat history deletion before editing another chat')
        if manifest.exists():
            pending = json.loads(manifest.read_text(encoding='utf-8'))
            if any(pending['body'][key] != body[key] for key in ('taskId', 'messageId')) or pending['body'].get('_operation') != body.get('_operation'):
                raise web.HTTPConflict(text='Retry the original chat history deletion to finish cleanup')
        else:
            replacement = await self.copy_history(ident, body, operation, key)
            return web.json_response(self.store.conversation(replacement))
        self.pending_history = True
        self.history_maintenance = True
        try:
            # A controller's native history lives in the guest as well as the
            # host journal. Erase that independent copy before reporting success.
            if hasattr(self.runtime, 'erase_history'):
                await self.runtime.erase_history(ident, self.store)
            pending['guestErased'] = True
            save_edit(manifest, pending)
            result = finish_delete(self.store, ident)
            self.pending_history = any((self.store.home / 'history-edits').glob('*.json'))
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise web.HTTPConflict(text='Chat history cleanup could not finish. Retry Delete from here. ' + str(error)) from None
        finally:
            self.history_maintenance = False
            self.mobile_replay.clear()
        if operation == 'regenerate':
            self.start_regeneration(ident)
            result = self.store.conversation(ident)
        return web.json_response(result)

    async def regenerate(self, request):
        from .history import operation_key
        from .reply_versions import prepare
        body = await request.json()
        if isinstance(body, dict):
            body = dict(body)
            confirm = body.pop('confirm', request.match_info['id'])
            if confirm != request.match_info['id']:
                raise ValueError('Confirm the selected chat ID')
        if not isinstance(body, dict) or set(body) - {'segmentId'} != {'taskId', 'messageId', 'submissionId'}:
            raise ValueError('Expected taskId, messageId, submissionId and optional segmentId')
        self.validate_submission_id(body['submissionId'])
        if not all(isinstance(body[key], str) and body[key] for key in ('taskId', 'messageId')):
            raise ValueError('Select a task and message in this chat')
        ident = request.match_info['id']
        body['_operation'] = 'regenerate'
        key = operation_key(ident, body, True)
        async def accept():
            async with self.history_copy_lock:
                await self.ensure_segments(ident)
                self.history_sources.add(ident)
                try:
                    def prepare_work():
                        worker = Store.worker_connection(self.store.home, self.store.guest_paths)
                        try:
                            return prepare(worker, ident, body, key)
                        finally:
                            worker.close()
                    branch, created = await asyncio.to_thread(prepare_work)
                    job = self.store.db.execute('SELECT * FROM regeneration_jobs WHERE branch_id=?', (branch,)).fetchone()
                    if not self.closing and job['task_id'] not in self.jobs and self.store.get(job['task_id'])['status'] == 'queued':
                        self.jobs[job['task_id']] = asyncio.create_task(self.run(job['task_id']))
                    self.finalize_regeneration(branch)
                    return branch
                finally:
                    self.history_sources.discard(ident)
        operation = self.history_operations.get(key)
        if operation is None:
            operation = asyncio.create_task(accept())
            self.history_operations[key] = operation
            operation.add_done_callback(lambda _: self.history_operations.pop(key, None))
        branch = await asyncio.shield(operation)
        return web.json_response(self.store.conversation(branch))

    def finalize_regeneration(self, branch):
        if branch in self.regeneration_finalizers:
            return
        async def finish():
            from .reply_versions import copy_history, publish
            delay = .05
            while not self.closing:
                row = self.store.db.execute('SELECT * FROM regeneration_jobs WHERE branch_id=?', (branch,)).fetchone()
                if not row or row['phase'] == 'ready':
                    return
                job = dict(row)
                try:
                    task = self.store.get(job['task_id'])
                    if task['status'] not in TERMINAL and not task['activeTurnId']:
                        await asyncio.sleep(.05)
                        continue
                    if job['phase'] == 'pending':
                        def copy_work():
                            worker = Store.worker_connection(self.store.home, self.store.guest_paths)
                            try:
                                copy_history(worker, job)
                            finally:
                                worker.close()
                        await asyncio.to_thread(copy_work)
                    if job['task_id'] in self.jobs:
                        await asyncio.sleep(.05)
                        continue
                    def publish_work():
                        worker = Store.worker_connection(self.store.home, self.store.guest_paths)
                        try:
                            publish(worker, job)
                        finally:
                            worker.close()
                    publication = asyncio.create_task(asyncio.to_thread(publish_work))
                    self.segment_operations[branch] = publication
                    try:
                        await asyncio.shield(publication)
                    finally:
                        self.segment_operations.pop(branch, None)
                    self.mobile_replay.clear()
                    if self.store.get(job['task_id'])['status'] == 'completed':
                        self.dispatch_followup(branch)
                    return
                except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as error:
                    self.store.segments.scope = None
                    with self.store.db:
                        self.store.db.execute('UPDATE regeneration_jobs SET error=? WHERE branch_id=?', (str(error), branch))
                    await asyncio.sleep(delay)
                    delay = min(5, delay * 2)
        job = asyncio.create_task(finish())
        self.regeneration_finalizers[branch] = job
        job.add_done_callback(lambda _: self.regeneration_finalizers.pop(branch, None))

    async def copy_history(self, ident, body, operation, key):
        from .history import build_replacement

        async def perform():
            async with self.history_copy_lock:
                await self.ensure_segments(ident)
                self.history_sources.add(ident)
                try:
                    replacement = await asyncio.to_thread(build_replacement, self.store.home,
                        self.store.guest_paths, ident, body, operation, key)
                    self.mobile_replay.clear()
                    if operation == 'regenerate' and not self.closing:
                        self.start_regeneration(replacement)
                    self.history_wakeup.set()
                    return replacement
                finally:
                    self.history_sources.discard(ident)

        job = self.history_operations.get(key)
        if job is None:
            job = asyncio.create_task(perform())
            self.history_operations[key] = job
            job.add_done_callback(lambda _: self.history_operations.pop(key, None))
        # A disconnected phone must not cancel a copy already accepted by the
        # host or leave regeneration waiting for another user click.
        return await asyncio.shield(job)

    def start_regeneration(self, ident):
        task = self.store.conversation_tasks(ident)[-1]
        if task['id'] in self.jobs or task['status'] != 'interrupted' or task['error'] != 'Regeneration ready. Retry Regenerate to start.':
            return
        with self.store.db:
            self.store.db.execute('UPDATE tasks SET created_at=? WHERE id=?', (int(time.time()), task['id']))
        self.store.update(task['id'], status='queued', error=None, result=None)
        self.jobs[task['id']] = asyncio.create_task(self.run(task['id']))

    async def history_maintenance_worker(self, app):
        from .secure_delete import defer_overwrites, drain_overwrites, erase_saved_data, pending_path

        async def reap():
            delay = .1
            while True:
                try:
                    await asyncio.wait_for(self.history_wakeup.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                self.history_wakeup.clear()
                delay = 5
                # Native databases are shared with model processes. Detach their
                # old records between turns; new chat work remains available.
                if self.jobs or self.steering_operations or self.history_maintenance or self.history_operations or self.segment_operations or self.regeneration_finalizers:
                    delay = .25
                    continue
                rows = self.store.db.execute('SELECT * FROM history_deletions').fetchall()
                partial = self.store.db.execute("SELECT conversation_id FROM tasks WHERE idempotency_key LIKE 'history-replacement:staging:%'").fetchall()
                for row in partial:
                    try:
                        journal = self.store.home / 'chat-segments' / ('branch-' + row[0] + '.json')
                        if journal.exists():
                            saved = json.loads(journal.read_text(encoding='utf-8'))
                            self.store.segments.references(row[0], saved['source'], recovering=saved)
                        with defer_overwrites():
                            erase_saved_data(self.store, [row[0]])
                    except (OSError, ValueError, KeyError, sqlite3.Error, RuntimeError):
                        continue
                for row in rows:
                    ident = row['source_id']
                    try:
                        if not row['guest_erased']:
                            if hasattr(self.runtime, 'erase_history'):
                                await self.runtime.erase_history(ident, self.store)
                            with self.store.db:
                                self.store.db.execute('UPDATE history_deletions SET guest_erased=1 WHERE source_id=?', (ident,))
                        if self.jobs or self.steering_operations or self.history_maintenance or self.history_operations or self.segment_operations or self.regeneration_finalizers:
                            delay = .25
                            break
                        exists = self.store.db.execute('SELECT 1 FROM tasks WHERE conversation_id=?', (ident,)).fetchone()
                        if exists or pending_path(self.store.home, ident).exists():
                            # This short preparation uses the service connection
                            # without yielding. Seven-pass I/O runs off the loop
                            # against retired files, never against a live DB.
                            with defer_overwrites():
                                erase_saved_data(self.store, [ident])
                        self.mobile_replay.clear()
                        await self.drain_history_overwrites(drain_overwrites)
                        with self.store.db:
                            self.store.db.execute('DELETE FROM history_deletions WHERE source_id=?', (ident,))
                    except (OSError, ValueError, sqlite3.Error, RuntimeError):
                        # Both the database queue and detached files survive a
                        # restart. Failure never invalidates the replacement.
                        continue
                # Recover files detached immediately before a process stopped,
                # including cleanup whose source records are already gone.
                try:
                    await self.drain_history_overwrites(drain_overwrites)
                except (OSError, ValueError):
                    pass

        self.history_reaper = asyncio.create_task(reap())

    async def drain_history_overwrites(self, drain):
        self.overwrite_job = asyncio.create_task(asyncio.to_thread(drain, self.store.home))
        try:
            await asyncio.shield(self.overwrite_job)
        finally:
            if self.overwrite_job.done():
                self.overwrite_job = None

    async def edit_message(self, request):
        from .history import edit_message
        body = await library_body(request, {'taskId', 'messageId', 'submissionId', 'text'})
        self.validate_submission_id(body['submissionId'])
        if not all(isinstance(body[key], str) and body[key] for key in ('taskId', 'messageId')):
            raise ValueError('Select a task and message in this chat')
        ident = request.match_info['id']
        if body.get('segmentId'):
            from .history import operation_key
            key = operation_key(ident, {**body, '_operation': 'edit-message'})
            existing = self.store.db.execute('SELECT conversation_id FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
            if existing:
                return web.json_response(self.store.conversation(existing[0]))
        if body.get('segmentId') and self.store.segments.info(ident, body['segmentId'])['id'] != self.store.segments.info(ident)['id']:
            from .history import operation_key
            body['_operation'] = 'edit-message'
            replacement = await self.copy_history(ident, body, 'edit-message', operation_key(ident, body))
            return web.json_response(self.store.conversation(replacement))
        task_ids = {task['id'] for task in self.store.conversation_tasks(ident)}
        steering = self.store.db.execute('SELECT id,task_id FROM steering').fetchall() if self.steering_operations else []
        if task_ids.intersection(self.jobs) or any(row['task_id'] in task_ids and row['id'] in self.steering_operations for row in steering):
            raise web.HTTPConflict(text='Finish or cancel this chat before editing its history')
        try:
            result = edit_message(self.store, ident, body)
        finally:
            self.store.segments.scope = None
        self.mobile_replay.clear()
        return web.json_response(result)

    async def shutdown(self, request):
        asyncio.get_running_loop().call_later(0.1, self.stopped.set)
        return web.json_response({"stopping": True})

    async def list_tasks(self, request):
        return web.json_response(
            self.store.list(
                request.query.get("cursor"),
                min(100, max(1, int(request.query.get("limit", 50)))),
                request.query.get("workspaceId"),
            )
        )

    def submit(self, body, key=None):
        if getattr(self.runtime, 'role', None) == 'controller' and self.runtime.state.get('phase') == 'exported':
            raise web.HTTPConflict(text='This environment is fenced for export')
        if getattr(self.runtime, 'role', None) == 'controller' and self.runtime.state.get('phase') == 'quarantined':
            raise web.HTTPConflict(text=self.runtime.state.get('hostNotice') or 'The host could not confirm guest shutdown; reset the environment before submitting work')
        if isinstance(body, dict) and "submissionId" in body:
            body = dict(body)
            submission_id = body.pop("submissionId")
            self.validate_submission_id(submission_id)
            key = key or "submission:" + submission_id
        allowed = {"prompt", "workingDirectory", "resumeTaskId", "workspaceId", "attachments"} | CHAT_FIELDS
        if getattr(self.runtime, 'role', None) == 'guest':
            allowed.add('credentialWorkspaceId')
        if not isinstance(body, dict) or body.keys() - allowed:
            raise ValueError(
                "Expected prompt, workspaceId, workingDirectory, browserMode, browser, browserProfile, and optional resumeTaskId"
            )
        if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
            raise ValueError("A nonempty prompt is required")
        if body.get('credentialWorkspaceId') is not None:
            identifier(body['credentialWorkspaceId'])
        if len(body["prompt"].encode()) > 4 * 1024 * 1024:
            raise ValueError("Prompt exceeds the 4 MiB submission limit")
        if key:
            existing = self.store.db.execute("SELECT id,request FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                saved = self.store.get(existing['id'])['request']
                supplied = {**body}
                if isinstance(supplied.get("workingDirectory"), str):
                    if self.store.guest_paths:
                        supplied["workingDirectory"] = directory_key(supplied["workingDirectory"], True)
                        saved["workingDirectory"] = directory_key(saved["workingDirectory"], True)
                    else:
                        supplied["workingDirectory"] = str(Path(supplied["workingDirectory"]).resolve())
                if any(saved.get(field) != value for field, value in supplied.items()):
                    raise ValueError("Idempotency key was already used for a different request")
                return self.store.get(existing["id"]), False
        parent = self.store.get(body["resumeTaskId"]) if body.get("resumeTaskId") else None
        if parent:
            from .reply_versions import pending
            if pending(self.store, parent['conversationId']):
                raise web.HTTPConflict(text='Response branch preparation is in progress; retry shortly')
        if parent and parent['conversationId'] in self.history_sources:
            raise web.HTTPConflict(text='This chat is being updated. Continue in its replacement when it is ready.')
        if parent and self.store.db.execute('SELECT 1 FROM history_deletions WHERE source_id=?', (parent['conversationId'],)).fetchone():
            raise web.HTTPGone(text='This chat has been replaced. Continue in the updated chat.')
        workspace_id = body.get("workspaceId") or (parent["workspaceId"] if parent else None)
        if parent and workspace_id != parent["workspaceId"]:
            raise ValueError("A conversation continuation must remain in its workspace")
        if parent:
            body = {**{field: parent["request"][field] for field in CHAT_FIELDS if field in parent["request"]}, **body}
        workspace = self.store.workspace(workspace_id) if workspace_id else None
        directory_value = body.get("workingDirectory", workspace["workingDirectory"] if workspace else (r'C:\workspaces' if self.store.guest_paths else ''))
        if not isinstance(directory_value, str):
            raise ValueError("workingDirectory must be a string")
        directory = PureWindowsPath(normalize_guest_directory(directory_value)) if self.store.guest_paths else Path(directory_value)
        if not directory.is_absolute() or (not self.store.guest_paths and not directory.is_dir()):
            raise ValueError(
                "workingDirectory must name an existing absolute directory on the execution host"
            )
        mode = body.get("browserMode", workspace["browserMode"] if workspace else "dedicated")
        if mode not in {"dedicated", "attached"}:
            raise ValueError("browserMode must be dedicated or attached")
        if workspace is None:
            workspace = self.store.workspace_for_directory(str(directory if self.store.guest_paths else directory.resolve()), mode)
        body = {
            **body,
            "workingDirectory": str(directory if self.store.guest_paths else directory.resolve()),
            "browserMode": mode,
            "workspaceId": workspace["id"],
            **browser_settings({**workspace, **body}),
            **chat_settings(body),
        }
        if parent and (
            parent["status"] not in TERMINAL
            or (not parent["threadId"] and self.store.history_seed(parent['id']) is None)
            or parent["id"] in self.jobs
        ):
            raise ValueError(
                "Only a finished task with a saved thread can be continued"
            )
        parent_owner = (parent['threadId'] or 'history:' + parent['conversationId']) if parent else None
        if parent and self.store.history_seed(parent['id']) is not None and self.store.conversation_tasks(parent['conversationId'])[-1]['id'] != parent['id']:
            raise ValueError('Continue the latest task in this chat')
        if parent and parent_owner in self.thread_owners:
            raise ValueError(
                "This conversation already has a queued or running continuation"
            )
        if len(self.jobs) >= 102:
            raise web.HTTPTooManyRequests(
                text="Two workers and the 100-task queue are occupied"
            )
        self.attachments.validate(body.get('attachments', []), workspace['id'], parent['conversationId'] if parent else None)
        task, created = self.store.submit(body, key)
        if created:
            self.attachments.claim(body.get('attachments', []), task['id'])
            if parent:
                self.store.update(task["id"], thread_id=parent["threadId"])
                self.thread_owners[parent_owner] = task["id"]
            self.jobs[task["id"]] = asyncio.create_task(self.run(task["id"]))
        return self.store.get(task["id"]), created

    async def run(self, ident, resuming=False):
        try:
            async with self.slots:
                self.store.update(ident, status="running")
                task = self.store.get(ident)
                if resuming == "stream" and task["threadId"]:
                    from .stream_recovery import RECOVERY_PROMPT
                    task["resumePrompt"] = RECOVERY_PROMPT
                    task["streamRecoveryRequested"] = True
                elif resuming and task["threadId"]:
                    task["resumePrompt"] = "Continue the unfinished task from the last saved activity. Do not repeat completed commands or browser actions. Original request: " + task["request"]["prompt"]
                await self.runtime.run(task, self.store)
        except asyncio.CancelledError:
            status = self.store.get(ident)["status"]
            disk_notice = getattr(self.runtime, 'state', {}).get('hostNotice')
            if status not in TERMINAL | {'paused'} and disk_notice and status != 'cancelling':
                self.store.update(ident, status='failed', error=disk_notice)
            elif status not in TERMINAL | {'paused'}:
                self.store.update(ident, status="cancelled")
            raise
        except Exception as error:
            self.store.update(ident, status="failed", error=str(error))
        finally:
            await self.browser.release_task(ident)
            self.jobs.pop(ident, None)
            for thread, owner in list(self.thread_owners.items()):
                if owner == ident and self.store.get(ident)["status"] != "paused":
                    del self.thread_owners[thread]
            if not self.closing:
                conversation = self.store.get(ident)["conversationId"]
                if self.store.get(ident)["status"] in {"failed", "cancelled", "interrupted"}:
                    self.store.hold_queue(conversation)
                elif self.store.get(ident)["status"] == "completed":
                    from .reply_versions import pending
                    if not pending(self.store, conversation):
                        self.dispatch_followup(conversation)

    async def create(self, request):
        body = await request.json()
        seed = None
        if isinstance(body, dict) and 'historyImport' in body and getattr(self.runtime, 'role', None) == 'guest':
            body = dict(body)
            seed = body.pop('historyImport')
            if not isinstance(seed, list) or any(not isinstance(message, dict) or message.keys() != {'role', 'text', 'attachments'}
                or message['role'] not in {'user', 'assistant'} or not isinstance(message['text'], str)
                or not isinstance(message['attachments'], list) for message in seed):
                raise ValueError('Invalid imported chat history')
            for message in seed:
                self.attachments.validate(message['attachments'], body.get('workspaceId'))
        task, created = self.submit(
            body, request.headers.get("Idempotency-Key")
        )
        if seed is not None and created:
            self.store.set_history_seed(task['id'], seed)
            for message in seed:
                self.attachments.claim(message['attachments'], task['id'])
        return web.json_response(task, status=202 if created else 200)

    async def get(self, request):
        task = self.store.get(request.match_info["id"])
        if hasattr(self.runtime, 'metadata'):
            task['execution'] = self.runtime.metadata(task['id'])
        directory = self.store.home / "artifacts" / task["id"]
        task["artifacts"] = [
            {"id": path.name, "size": path.stat().st_size}
            for path in directory.glob("*")
            if path.is_file() and not path.is_symlink()
        ]
        return web.json_response(task)

    async def cancel(self, request):
        ident = request.match_info["id"]
        self.store.get(ident)
        # Client disconnects must not abandon cancellation during worker cleanup.
        operation = self.cancellations.get(ident)
        if operation is None or operation.done():
            operation = asyncio.create_task(self.cancel_task(ident))
            self.cancellations[ident] = operation
        return web.json_response(await asyncio.shield(operation))

    async def cancel_task(self, ident):
        task = self.store.get(ident)
        if task["status"] in TERMINAL:
            return task
        self.store.update(ident, status="cancelling")
        if job := self.jobs.get(ident):
            try:
                await self.runtime.interrupt(ident, self.store)
            except Exception as error:
                if getattr(self.runtime, 'role', None) == 'controller':
                    raise web.HTTPServiceUnavailable(text='Hyper-V could not confirm termination; task remains cancelling: ' + str(error)) from error
                # A failed native interrupt is not allowed to prevent process cleanup.
                pass
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.store.update(ident, status="cancelled")
        self.store.hold_queue(task["conversationId"])
        self.jobs.pop(ident, None)
        for thread, owner in list(self.thread_owners.items()):
            if owner == ident:
                del self.thread_owners[thread]
        return self.store.get(ident)

    async def pause(self, request):
        ident = request.match_info["id"]
        async with self.control_locks.setdefault(ident, asyncio.Lock()):
            task = self.store.get(ident)
            if task["status"] in {"paused", "pausing"}:
                return web.json_response(task)
            if task["status"] not in {"queued", "running"}:
                raise web.HTTPConflict(text="Only queued or running tasks can be paused")
            if task["status"] == "queued":
                self.store.update(ident, status="paused")
                if job := self.jobs.get(ident):
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
            else:
                self.store.update(ident, status="pausing")
                await self.runtime.pause(ident, self.store)
            return web.json_response(self.store.get(ident))

    async def resume(self, request):
        ident = request.match_info["id"]
        async with self.control_locks.setdefault(ident, asyncio.Lock()):
            task = self.store.get(ident)
            if task["status"] not in {"paused", "pausing"}:
                raise web.HTTPConflict(text="Only paused tasks can be resumed")
            if ident in self.jobs:
                await self.runtime.resume(ident, self.store)
                self.store.update(ident, status="running")
            else:
                if task["threadId"]:
                    owner = self.thread_owners.get(task["threadId"])
                    if owner and owner != ident:
                        raise web.HTTPConflict(text="This conversation already has an active task")
                    self.thread_owners[task["threadId"]] = ident
                self.store.update(ident, status="queued", error=None)
                self.jobs[ident] = asyncio.create_task(self.run(ident, resuming=True))
            return web.json_response(self.store.get(ident))

    async def retry_task(self, request):
        from .stream_recovery import MAX_STREAM_RECOVERY_ATTEMPTS
        if await request.read():
            if await request.json() != {}:
                raise ValueError("Retry does not accept a new prompt, attachments, or settings")
        ident = request.match_info["id"]
        async with self.control_locks.setdefault(ident, asyncio.Lock()):
            task = self.store.get(ident)
            if ident in self.jobs:
                if task["status"] in {"queued", "running", "pausing", "paused"}:
                    return web.json_response(task)
                raise web.HTTPConflict(text="Wait for the current worker to finish cleanup before retrying")
            if not task["canRetry"]:
                raise web.HTTPConflict(text="Only a recoverable failed response with a saved conversation can be retried")
            if self.store.conversation_tasks(task["conversationId"])[-1]["id"] != ident:
                raise web.HTTPConflict(text="Retry the latest task in this conversation")
            owner = self.thread_owners.get(task["threadId"])
            if owner and owner != ident:
                raise web.HTTPConflict(text="This conversation already has an active task")
            if len(self.jobs) >= 102:
                raise web.HTTPTooManyRequests(text="Two workers and the 100-task queue are occupied")
            self.store.event(ident, "recovery", {"phase": "requested", "attempt": 0,
                "maxAttempts": MAX_STREAM_RECOVERY_ATTEMPTS, "message": "Retry requested. Continuing from the saved conversation.", "error": task["error"]})
            self.thread_owners[task["threadId"]] = ident
            self.store.update(ident, status="queued", error=None)
            self.jobs[ident] = asyncio.create_task(self.run(ident, resuming="stream"))
            return web.json_response(self.store.get(ident), status=202)

    async def continue_task(self, request):
        ident = request.match_info["id"]
        previous, body = self.store.get(ident), await request.json()
        if not isinstance(body, dict) or "prompt" not in body or body.keys() - ({"prompt", "submissionId", "attachments"} | CHAT_FIELDS):
            raise ValueError("Continuation accepts prompt and chat browser, skills, mode, and goal settings")
        task, created = self.submit(
            {
                "workingDirectory": previous["request"]["workingDirectory"],
                "browserMode": previous["request"]["browserMode"],
                "browser": previous["request"].get("browser", "chromium"),
                "browserProfile": previous["request"].get("browserProfile", ""),
                "workspaceId": previous["workspaceId"],
                **body,
                "resumeTaskId": ident,
            },
            request.headers.get("Idempotency-Key"),
        )
        return web.json_response(task, status=202 if created else 200)

    @staticmethod
    def validate_submission_id(value):
        import uuid
        if not isinstance(value, str):
            raise ValueError("submissionId must be a UUID")
        try:
            uuid.UUID(value)
        except ValueError:
            raise ValueError("submissionId must be a UUID") from None

    @staticmethod
    def validate_prompt(body):
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 4 * 1024 * 1024:
            raise ValueError("A nonempty prompt of at most 4 MiB is required")

    async def list_conversations(self, request):
        await asyncio.gather(*list(self.segment_operations.values()), return_exceptions=True)
        return web.json_response(self.store.conversations(request.query.get("cursor"),
            min(100, max(1, int(request.query.get("limit", 50)))), request.query.get("workspaceId")))

    async def get_conversation(self, request):
        ident = request.match_info['id']
        await self.ensure_segments(ident)
        return web.json_response(self.store.conversation(ident, request.query.get('segmentId')))

    async def rename_conversation(self, request):
        body = await library_body(request, {"title"})
        return web.json_response(self.store.rename_conversation(request.match_info["id"], body["title"]))

    async def conversation_events(self, request):
        ident = request.match_info["id"]
        await self.ensure_segments(ident)
        segment_id = request.query.get('segmentId')
        after = int(request.headers.get("Last-Event-ID", request.query.get("after", 0)))
        if request.query.get("format") == "json" and request.query.get('view') == 'mobile':
            through = request.query.get('through')
            return web.json_response(self.mobile_replay.page(self.store, ident, after, int(through) if through else None, segment_id=segment_id))
        self.store.conversation_tasks(ident)
        if request.query.get("format") == "json":
            through = request.query.get("through")
            return web.json_response({"data": self.store.conversation_events(ident, after, int(through) if through else None, segment_id)})
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await stream.prepare(request)
        heartbeat = time.monotonic()
        try:
            # Conversation streams remain open across successor tasks.
            while not self.closing:
                publication = self.segment_operations.get(ident)
                if publication:
                    await asyncio.shield(publication)
                for event in self.store.conversation_events(ident, after):
                    await stream.write(f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n".encode())
                    after = event["id"]
                if time.monotonic() - heartbeat >= 15:
                    await stream.write(b": heartbeat\n\n")
                    heartbeat = time.monotonic()
                await asyncio.sleep(.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return stream

    async def steer_task(self, request):
        ident, body = request.match_info["id"], await request.json()
        if not isinstance(body, dict) or body.keys() - {"prompt", "expectedTurnId", "submissionId", "attachments"} or not {"prompt", "expectedTurnId", "submissionId"} <= body.keys():
            raise ValueError("Steering requires prompt, expectedTurnId, and submissionId")
        self.validate_prompt(body)
        self.validate_submission_id(body["submissionId"])
        if not isinstance(body["expectedTurnId"], str) or not body["expectedTurnId"]:
            raise ValueError("expectedTurnId must not be empty")
        self.store.get(ident)
        submission = body["submissionId"]
        async with self.delivery_locks.setdefault(ident, asyncio.Lock()):
            saved = self.store.db.execute("SELECT * FROM steering WHERE id=?", (submission,)).fetchone()
            if saved:
                if saved["task_id"] != ident or json.loads(saved["body"]) != body:
                    raise ValueError("submissionId was already used for another message")
                if saved["status"] == "accepted":
                    return web.json_response(json.loads(saved["response"]))
                if saved["status"] == "rejected":
                    raise web.HTTPConflict(text="Your message was not delivered; the active turn changed")
                operation = self.steering_operations.get(submission)
                if not operation:
                    raise web.HTTPConflict(text="Delivery could not be confirmed. Inspect the chat before resending.")
            else:
                task = self.store.get(ident)
                if task["status"] != "running" or task["activeTurnId"] != body["expectedTurnId"]:
                    raise web.HTTPConflict(text="Your message was not delivered; there is no matching active turn")
                self.attachments.validate(body.get('attachments', []), task['workspaceId'], task['conversationId'])
                self.attachments.claim(body.get('attachments', []), ident)
                with self.store.db:
                    self.store.db.execute("INSERT INTO steering VALUES (?,?,?,'pending',NULL)", (submission, ident, json.dumps(body)))
                operation = asyncio.create_task(self.deliver_steering(ident, body))
                self.steering_operations[submission] = operation
        result = await asyncio.shield(operation)
        return web.json_response(result)

    async def deliver_steering(self, ident, body):
        submission = body["submissionId"]
        try:
            result = await self.runtime.steer(ident, self.store, body)
            with self.store.db:
                self.store.db.execute("UPDATE steering SET status='accepted',response=? WHERE id=?", (json.dumps(result), submission))
                self.store.event(ident, "steering", {**body, "turnId": result["turnId"]})
                self.store.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (int(time.time()), ident))
            return result
        except ValueError as error:
            with self.store.db:
                self.store.db.execute("UPDATE steering SET status='rejected' WHERE id=?", (submission,))
            raise web.HTTPConflict(text="Your message was not delivered; the active turn is unavailable") from error
        except Exception as error:
            with self.store.db:
                self.store.db.execute("UPDATE steering SET status='uncertain' WHERE id=?", (submission,))
            raise web.HTTPConflict(text="Message delivery could not be confirmed. Inspect the chat before resending.") from error
        finally:
            self.steering_operations.pop(submission, None)

    async def queue_followup(self, request):
        ident, body = request.match_info["id"], await request.json()
        tasks = self.store.conversation_tasks(ident)
        if not isinstance(body, dict) or body.keys() - ({"prompt", "submissionId", "attachments"} | CHAT_FIELDS):
            raise ValueError("Queue accepts prompt, submissionId, and chat settings")
        self.validate_prompt(body)
        self.validate_submission_id(body.get("submissionId"))
        saved = self.store.db.execute("SELECT * FROM followups WHERE id=?", (body["submissionId"],)).fetchone()
        if saved:
            original = json.loads(saved["request"] or saved["body"])
            matches = original == body if saved["request"] else all(original.get(key) == value for key, value in body.items())
            if saved["conversation_id"] != ident or not matches:
                raise ValueError("submissionId was already used for another follow-up")
            return web.json_response(self.store.conversation(ident), status=202)
        # Validate overrides now; capture workspace instructions when dispatched.
        normalized = {**body, **chat_settings({**tasks[-1]["request"], **body}), **browser_settings({**tasks[-1]["request"], **body})}
        if "skills" not in body:
            normalized.pop("skills", None)
        self.attachments.validate(body.get('attachments', []), tasks[-1]['workspaceId'], ident)
        self.attachments.claim(body.get('attachments', []), tasks[-1]['id'])
        with self.store.db:
            self.store.db.execute("INSERT INTO followups(id,conversation_id,body,request,created_at) VALUES (?,?,?,?,?)",
                (body["submissionId"], ident, json.dumps(normalized), json.dumps(body), int(time.time())))
        self.store.event(tasks[-1]["id"], "queue", {"submissionId": body["submissionId"], "status": "waiting", "body": normalized})
        if tasks[-1]["status"] in {"failed", "cancelled", "interrupted"}:
            self.store.hold_queue(ident)
        self.dispatch_followup(ident)
        return web.json_response(self.store.conversation(ident), status=202)

    def dispatch_followup(self, ident):
        from .reply_versions import pending
        if pending(self.store, ident):
            return
        if self.closing:
            return
        chat = self.store.conversation(ident)
        task = chat["tasks"][-1]
        if chat["queueHeld"] or not chat["followups"] or task["status"] not in TERMINAL or task["id"] in self.jobs:
            return
        followup = chat["followups"][0]
        try:
            successor, _ = self.submit({**followup["body"], "workspaceId": task["workspaceId"],
                "workingDirectory": task["request"]["workingDirectory"], "resumeTaskId": task["id"]}, "followup:" + followup["id"])
        except (ValueError, KeyError, web.HTTPException):
            self.store.hold_queue(ident)
            return
        with self.store.db:
            self.store.db.execute("UPDATE followups SET status='dispatched',task_id=? WHERE id=?", (successor["id"], followup["id"]))
        self.store.event(successor["id"], "queue", {"submissionId": followup["id"], "status": "dispatched"})

    async def remove_followup(self, request):
        ident, followup = request.match_info["id"], request.match_info["followupId"]
        tasks = self.store.conversation_tasks(ident)
        with self.store.db:
            removed = self.store.db.execute("UPDATE followups SET status='removed' WHERE id=? AND conversation_id=? AND status='waiting'", (followup, ident)).rowcount
        if not removed:
            raise web.HTTPConflict(text="This follow-up is no longer waiting")
        self.store.event(tasks[-1]["id"], "queue", {"submissionId": followup, "status": "removed"})
        return web.json_response(self.store.conversation(ident))

    async def resume_queue(self, request):
        ident = request.match_info["id"]
        self.store.conversation_tasks(ident)
        self.store.hold_queue(ident, False)
        self.dispatch_followup(ident)
        return web.json_response(self.store.conversation(ident))

    async def list_workspaces(self, request):
        return web.json_response(self.store.workspaces())

    async def get_workspace(self, request):
        return web.json_response(self.store.workspace(request.match_info["id"]))

    async def create_workspace(self, request):
        return web.json_response(self.store.save_workspace(await request.json()), status=201)

    async def update_workspace(self, request):
        return web.json_response(self.store.save_workspace(await request.json(), request.match_info["id"]))

    async def skills(self, request):
        workspace_id = request.query.get("workspaceId")
        workspace = self.store.workspace(workspace_id) if workspace_id else None
        directory = request.query.get("workingDirectory") or (workspace["workingDirectory"] if workspace else None)
        if self.store.guest_paths:
            directory = None
        registered = self.skill_categories()
        assigned = set(registered) | set(workspace["skills"] if workspace else ())
        if not workspace:
            for item in self.store.workspaces()["data"]:
                assigned.update(item["skills"])
        result = await asyncio.to_thread(skill_catalog, self.store.home, directory, assigned, workspace["skills"] if workspace else ())
        for item in result["data"]:
            item["category"] = registered.get(item["path"], "Uncategorized")
        return web.json_response(result)

    def skill_categories(self):
        return {row["path"]: row["category"] for row in self.store.db.execute("SELECT * FROM skill_library")}

    def register_skills(self, paths, category=None):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 2048:
            raise ValueError("Select between 1 and 2048 skills")
        documents = [skill_document(path) for path in paths]
        normalized = category_name(category) if category is not None else None
        with self.store.db:
            for document in documents:
                if normalized is None:
                    self.store.db.execute("INSERT OR IGNORE INTO skill_library VALUES (?,?)", (document["path"], "Uncategorized"))
                else:
                    self.store.db.execute("INSERT INTO skill_library VALUES (?,?) ON CONFLICT(path) DO UPDATE SET category=excluded.category", (document["path"], normalized))
        return {"data": [{"path": item["path"]} for item in documents]}

    async def skill_detail(self, request):
        document = skill_document(request.query.get("path"))
        return web.json_response({**document, "category": self.skill_categories().get(document["path"], "Uncategorized")})

    async def add_skills(self, request):
        body = await library_body(request, {"paths"})
        return web.json_response(self.register_skills(body.get("paths")))

    async def categorize_skills(self, request):
        body = await library_body(request, {"paths", "category"})
        return web.json_response(self.register_skills(body.get("paths"), category_name(body.get("category"))))

    async def export_skills(self, request):
        body = await library_body(request, {"paths"})
        content = await asyncio.to_thread(export_bundle, body.get("paths"), self.skill_categories())
        return web.Response(body=content, content_type="application/zip", headers={"Content-Disposition": 'attachment; filename="skills.zip"'})

    async def import_skills(self, request):
        body = await library_body(request, {"file"})
        items = await asyncio.to_thread(import_bundle, self.store.home, body.get("file"))
        with self.store.db:
            for item in items:
                self.store.db.execute("INSERT INTO skill_library VALUES (?,?)", (item["path"], item["category"]))
        return web.json_response({"data": items}, status=201)

    async def browsers(self, request):
        if getattr(self.runtime, 'role', None) == 'controller':
            return web.json_response(await self.runtime.transport.request('GET', '/v1/browsers'))
        result = browser_catalog(self.store.home, self.browser)
        for browser in result["data"]:
            browser["profiles"] = sorted(set(browser["profiles"]) | {
                workspace["browserProfile"] for workspace in self.store.workspaces()["data"]
                if workspace["browserMode"] == "dedicated" and workspace["browser"] == browser["id"] and workspace["browserProfile"]})
        return web.json_response(result)

    async def connect_browser_profile(self, request):
        body = await library_body(request, {"browser", "profileId"})
        if getattr(self.runtime, 'role', None) == 'controller':
            return web.json_response(await self.runtime.transport.request('POST', '/v1/browsers/connect', body))
        return web.json_response(open_installed_profile(body["browser"], body["profileId"]))

    async def setup_browser_profile(self, request):
        body = await library_body(request, {"browser", "profileId", "confirmed"})
        if getattr(self.runtime, 'role', None) == 'controller':
            return web.json_response(await self.runtime.transport.request('POST', '/v1/browsers/setup', body))
        if body["confirmed"] is not True:
            raise ValueError("Confirm browser extension setup before making changes")
        async with self.browser_setup_lock:
            result = await asyncio.to_thread(setup_installed_profile, body["browser"], body["profileId"])
            for attempt in range(40):
                if any(item.get("browser") == body["browser"] and item.get("localProfileId") == body["profileId"] for item in self.browser.catalog()):
                    return web.json_response({**result, "connected": True})
                await asyncio.sleep(.25)
            raise ValueError("The extension was loaded and paired, but its connection did not arrive. Check the browser's extension permissions and try again.")

    async def events(self, request):
        ident = request.match_info["id"]
        self.store.get(ident)
        after = int(request.headers.get("Last-Event-ID", request.query.get("after", 0)))
        stream = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await stream.prepare(request)
        heartbeat = time.monotonic()
        try:
            while True:
                records = self.store.events(ident, after)
                for event in records:
                    await stream.write(
                        f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
                    after = event["id"]
                if not records and self.store.get(ident)["status"] in TERMINAL:
                    break
                if time.monotonic() - heartbeat >= 15:
                    await stream.write(b": heartbeat\n\n")
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return stream

    async def artifact(self, request):
        ident = request.match_info["id"]
        self.store.get(ident)
        root = (self.store.home / "artifacts" / ident).resolve()
        target = (root / request.match_info["artifactId"]).resolve()
        if target.parent != root or not target.is_file() or target.is_symlink():
            raise KeyError("artifact")
        return web.FileResponse(target, headers={"Content-Disposition": "attachment"})

    async def upload_attachment(self, request):
        return web.json_response(self.attachments.upload(await request.json()), status=201)

    async def attachment_info(self, request):
        row = self.attachments.get(request.match_info['id'])
        result = self.attachments.descriptor(row)
        if row['mime'].startswith('image/'):
            import base64
            result['preview'] = 'data:' + row['mime'] + ';base64,' + base64.b64encode(self.attachments.path(row['id']).read_bytes()).decode()
        return web.json_response(result)

    async def attachment_content(self, request):
        row = self.attachments.get(request.match_info['id'])
        return web.FileResponse(self.attachments.path(row['id']), headers={'Content-Disposition': 'attachment', 'Content-Type': row['mime']})

    async def remove_attachment(self, request):
        self.attachments.remove_draft(request.match_info['id'])
        return web.json_response({'removed': True})

    async def begin_shutdown(self, app):
        # End live SSE handlers before aiohttp waits for open requests to drain.
        # Waiting until on_cleanup leaves clients and database handles open for
        # the entire default HTTP shutdown timeout.
        self.closing = True

    async def cleanup(self, app):
        self.closing = True
        await asyncio.gather(*list(self.regeneration_finalizers.values()), return_exceptions=True)
        if self.segment_reaper:
            self.segment_reaper.cancel()
            await asyncio.gather(self.segment_reaper, return_exceptions=True)
        await asyncio.gather(*list(self.segment_operations.values()), return_exceptions=True)
        await asyncio.gather(*list(self.history_operations.values()), return_exceptions=True)
        if self.history_reaper:
            self.history_reaper.cancel()
            await asyncio.gather(self.history_reaper, return_exceptions=True)
        if self.overwrite_job:
            await asyncio.gather(self.overwrite_job, return_exceptions=True)
        if self.credential_reaper:
            self.credential_reaper.cancel()
            await asyncio.gather(self.credential_reaper, return_exceptions=True)
        self.credentials.close()
        if getattr(self.runtime, 'role', None) == 'controller':
            await self.runtime.close()
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(*self.cancellations.values(), return_exceptions=True)
        await asyncio.gather(*self.steering_operations.values(), return_exceptions=True)
        await asyncio.gather(
            *(socket.close() for socket in list(self.browser.connections.values()))
        )
        await self.runtime.close()
        self.store.close()
        self.auth.close()

    async def environment(self, request):
        if getattr(self.runtime, 'role', None) != 'controller':
            raise web.HTTPNotFound(text='Environment controls exist only on the host controller')
        action = request.match_info.get('action', 'status')
        operations = {'status': 'status', 'pause': 'pause', 'resume': 'resume', 'reset': 'restore', 'export': 'export'}
        if action not in operations:
            raise web.HTTPNotFound(text='Unknown environment action')
        return web.json_response(await self.runtime.environment(operations[action]))

    async def recover_execution(self, app):
        for row in self.store.db.execute("SELECT branch_id FROM regeneration_jobs WHERE phase!='ready'").fetchall():
            self.finalize_regeneration(row[0])
        if getattr(self.runtime, 'role', None) != 'controller':
            return
        await self.runtime.start_monitor()
        if self.runtime.state.get('phase') in {'exported', 'quarantined'}:
            return
        for row in self.store.db.execute("SELECT task_id FROM vm_attempts WHERE phase NOT IN ('completed','cancelled','failed')").fetchall():
            task = self.store.get(row['task_id'])
            if task['status'] in {'interrupted', 'running', 'queued'}:
                self.store.update(task['id'], status='queued')
                self.jobs[task['id']] = asyncio.create_task(self.run(task['id'], resuming=True))

    async def extension_connection(self, request):
        if getattr(self.runtime, 'role', None) == 'controller':
            raise web.HTTPForbidden(text='Host browsers are outside the guest execution environment')
        return await self.browser.extension(request)

    async def browser_command(self, request):
        if getattr(self.runtime, 'role', None) == 'controller':
            raise web.HTTPForbidden(text='Browser commands execute inside the guest only')
        return await self.browser.command(request)

    async def credential_maintenance(self, app):
        async def reap():
            while True:
                await asyncio.sleep(5)
                self.credentials.prune()
        self.credential_reaper = asyncio.create_task(reap())

    async def temporary_credentials(self, request):
        body = await request.json() if request.method == 'POST' else None
        workspace = identifier(body.get('workspaceId') if isinstance(body, dict) else request.query.get('workspaceId'))
        # Validate before the Hyper-V transport can stage a request on host disk.
        if request.method == 'POST':
            if request.path.endswith('/prepare'):
                if set(body) != {'workspaceId'}:
                    raise ValueError('Expected workspaceId')
            else:
                validate_envelope(body)
        controller = getattr(self.runtime, 'role', None) == 'controller'
        if controller:
            self.store.workspace(workspace)
            # The host forwards ciphertext only. Keys and plaintext stay in the guest.
            result = await self.runtime.transport.request(request.method, request.raw_path, body)
        elif request.method == 'DELETE':
            if 'id' in request.match_info:
                self.credentials.remove(identifier(request.match_info['id']), workspace)
            else:
                self.credentials.clear_workspace(workspace)
            result = {'revoked': True}
        elif request.method == 'GET':
            result = self.credentials.list(workspace)
        elif request.path.endswith('/prepare'):
            result = self.credentials.prepare(workspace)
        else:
            result = self.credentials.accept(body)
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    async def browser_credentials(self, request):
        if getattr(self.runtime, 'role', None) == 'controller':
            raise web.HTTPForbidden(text='Credentials are used inside the guest only')
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {'taskId', 'operation', 'credentialId', 'url', 'formOrigins'}:
            raise ValueError('Invalid browser credential request')
        task = self.store.get(body['taskId'])
        if task['status'] != 'running':
            raise ValueError('Credentials require a running task')
        workspace = task['request'].get('credentialWorkspaceId', task['workspaceId'])
        if body.get('operation') == 'list':
            result = self.credentials.list(workspace)
        elif body.get('operation') == 'fill':
            result = self.credentials.redeem(identifier(body.get('credentialId')), workspace, body.get('url'), body.get('formOrigins'))
        else:
            raise ValueError('Unsupported browser credential operation')
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    def app(self):
        app = web.Application(
            middlewares=[self.authenticate], client_max_size=23 * 1024 * 1024,
            handler_args=http_server_options(),
        )
        for method, route, handler in [
            ("GET", "/v1/health", self.health),
            ('GET', '/v1/environment', self.environment),
            ('POST', '/v1/environment/{action}', self.environment),
            ('POST', '/v1/attachments', self.upload_attachment),
            ('GET', '/v1/attachments/{id}', self.attachment_info),
            ('GET', '/v1/attachments/{id}/content', self.attachment_content),
            ('DELETE', '/v1/attachments/{id}', self.remove_attachment),
            ('POST', '/v1/credentials/prepare', self.temporary_credentials),
            ('POST', '/v1/credentials', self.temporary_credentials),
            ('GET', '/v1/credentials', self.temporary_credentials),
            ('DELETE', '/v1/credentials', self.temporary_credentials),
            ('DELETE', '/v1/credentials/{id}', self.temporary_credentials),
            ('POST', '/v1/browser/credentials', self.browser_credentials),
            ("GET", "/v1/workspaces", self.list_workspaces),
            ("POST", "/v1/workspaces", self.create_workspace),
            ("GET", "/v1/workspaces/{id}", self.get_workspace),
            ("PATCH", "/v1/workspaces/{id}", self.update_workspace),
            ("POST", "/v1/workspaces/{id}/secure-delete", self.secure_delete),
            ("GET", "/v1/skills", self.skills),
            ("GET", "/v1/skills/document", self.skill_detail),
            ("POST", "/v1/skills/register", self.add_skills),
            ("PATCH", "/v1/skills/categories", self.categorize_skills),
            ("POST", "/v1/skills/export", self.export_skills),
            ("POST", "/v1/skills/import", self.import_skills),
            ("GET", "/v1/browsers", self.browsers),
            ("POST", "/v1/browsers/connect", self.connect_browser_profile),
            ("POST", "/v1/browsers/setup", self.setup_browser_profile),
            ("GET", "/v1/tasks", self.list_tasks),
            ("GET", "/v1/chats", self.list_conversations),
            ("GET", "/v1/chats/{id}", self.get_conversation),
            ("PATCH", "/v1/chats/{id}", self.rename_conversation),
            ("POST", "/v1/chats/{id}/fork", self.edit_history),
            ("POST", "/v1/chats/{id}/delete-from", self.edit_history),
            ("POST", "/v1/chats/{id}/delete-message", self.edit_history),
            ("POST", "/v1/chats/{id}/regenerate", self.edit_history),
            ("POST", "/v1/chats/{id}/edit-message", self.edit_message),
            ("POST", "/v1/chats/{id}/secure-delete", self.secure_delete),
            ("GET", "/v1/chats/{id}/events", self.conversation_events),
            ("POST", "/v1/chats/{id}/followups", self.queue_followup),
            ("DELETE", "/v1/chats/{id}/followups/{followupId}", self.remove_followup),
            ("POST", "/v1/chats/{id}/followups/resume", self.resume_queue),
            ("POST", "/v1/tasks", self.create),
            ("GET", "/v1/tasks/{id}", self.get),
            ("GET", "/v1/tasks/{id}/events", self.events),
            ("POST", "/v1/tasks/{id}/cancel", self.cancel),
            ("POST", "/v1/tasks/{id}/pause", self.pause),
            ("POST", "/v1/tasks/{id}/resume", self.resume),
            ("POST", "/v1/tasks/{id}/retry", self.retry_task),
            ("POST", "/v1/tasks/{id}/continue", self.continue_task),
            ("POST", "/v1/tasks/{id}/steer", self.steer_task),
            ("GET", "/v1/tasks/{id}/artifacts/{artifactId}", self.artifact),
            ("GET", "/v1/browser/extension", self.extension_connection),
            ("POST", "/v1/browser/command", self.browser_command),
            ("POST", "/v1/service/shutdown", self.shutdown),
        ]:
            app.router.add_route(method, route, handler)
        app.router.add_get("/v1/openapi.json", self.openapi)
        app.on_shutdown.append(self.begin_shutdown)
        app.on_cleanup.append(self.cleanup)
        app.on_startup.append(self.recover_execution)
        app.on_startup.append(self.credential_maintenance)
        app.on_startup.append(self.history_maintenance_worker)
        app.on_startup.append(self.segment_maintenance)
        return app
