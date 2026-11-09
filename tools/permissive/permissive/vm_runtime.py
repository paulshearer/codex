"""Host-owned execution journal and Hyper-V transport. Never launches an agent locally."""

import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import time
import tempfile
import hashlib
import uuid

from .store import TERMINAL
from .process_tree import ProcessTree


class GuestProtocolError(RuntimeError):
    """Untrusted guest output is invalid; restarting the guest cannot fix it."""


class HostBudgetError(RuntimeError):
    """A host-owned resource limit was reached; do not retry or reset its ledger."""


class HostDiskCapacityError(HostBudgetError):
    """The host storage reserve fenced this environment until an explicit reset."""


class LifecycleLock(asyncio.Lock):
    """Let the capacity watchdog abort a blocked helper before acquiring the lock."""
    owner = None

    async def acquire(self):
        await super().acquire()
        self.owner = asyncio.current_task()
        return True

    def release(self):
        self.owner = None
        super().release()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def deployment(path):
    path = Path(path).resolve(strict=True)
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('role') not in {'controller', 'guest'}:
        raise ValueError('Deployment role must be controller or guest')
    if value['role'] == 'controller':
        uuid.UUID(value['vmId'])
        uuid.UUID(value.get('sandboxId', value['vmId']))
        if not value.get('guestCredentialPath'):
            raise ValueError('Controller deployment requires guestCredentialPath')
    return value


class HyperVTransport:
    """Only fixed host scripts/cmdlets run here; model commands remain guest data."""
    def __init__(self, binding_path):
        self.binding_path = Path(binding_path).resolve(strict=True)
        self.binding = deployment(self.binding_path)
        from .__main__ import package_root
        self.package = Path(self.binding.get('packagePath', package_root()))

    async def _process(self, *arguments, timeout=180):
        process = await asyncio.create_subprocess_exec(
            'powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
            *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        process_tree = None
        workers, group = [], None
        try:
            # PowerShell Direct may assemble a remote object before stdout is
            # emitted, so pipe limits alone cannot bound the host helper's RAM.
            process_tree = ProcessTree(process.pid, memory_limit=1024**3)
            async def read_limited(stream, maximum):
                chunks, size = [], 0
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        return b''.join(chunks)
                    size += len(chunk)
                    if size > maximum:
                        raise HostBudgetError('Hyper-V response exceeded the host transport budget')
                    chunks.append(chunk)
            workers = [asyncio.create_task(read_limited(process.stdout, 64*1024*1024)),
                       asyncio.create_task(read_limited(process.stderr, 64*1024)),
                       asyncio.create_task(process.wait())]
            group = asyncio.gather(*workers)
            stdout, stderr, _ = await asyncio.wait_for(group, timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            if group is not None:
                with contextlib.suppress(BaseException):
                    await group
            await process.wait()
            raise
        finally:
            if process_tree is not None:
                process_tree.close()
        if process.returncode:
            # Fixed helpers must never include credentials in errors.
            raise RuntimeError('Hyper-V transport failed: ' + stderr.decode('utf-8', 'replace').strip()[-1500:])
        try:
            return json.loads(stdout.decode('utf-8-sig').strip())
        except (ValueError, UnicodeError, RecursionError):
            raise GuestProtocolError('Hyper-V helper returned an invalid response') from None

    async def request(self, method, path, body=None, **options):
        request = {'operation': 'request', 'method': method, 'path': path, **options}
        if body is not None:
            request['body'] = body
        return await self.rpc(request)

    async def rpc(self, request):
        descriptor, name = tempfile.mkstemp(prefix='guest-request-', suffix='.json', dir=self.binding_path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(request, output, ensure_ascii=True)
            result = await self._process('-ExecutionPolicy', 'Bypass', '-File', str(
                self.package / 'windows/sandbox/Invoke-SandboxCommand.ps1'),
                '-BindingPath', str(self.binding_path), '-RequestPath', name)
        finally:
            Path(name).unlink(missing_ok=True)
        if not isinstance(result, dict) or result.get('protocol') != 'permissive-guest-bridge/v1' or type(result.get('ok')) is not bool:
            raise GuestProtocolError('Guest relay returned an invalid response envelope')
        if not result['ok']:
            error = result.get('error')
            if not isinstance(error, dict) or not isinstance(error.get('code'), str) or not isinstance(error.get('message'), str) or len(error['code']) > 128 or len(error['message']) > 2000:
                raise GuestProtocolError('Guest relay returned an invalid error envelope')
            raise RuntimeError(error['code'] + ': ' + error['message'])
        if 'result' not in result:
            raise GuestProtocolError('Guest relay omitted its result')
        return result['result']

    async def lifecycle(self, operation):
        if operation == 'restore':
            return await self._process('-ExecutionPolicy', 'Bypass', '-File', str(
                self.package / 'windows/sandbox/Restore-Sandbox.ps1'),
                '-BindingPath', str(self.binding_path), timeout=1800)
        # Identifiers are parsed UUIDs. No prompt, path, tool output, or user code
        # is interpolated into this host-side command.
        ident = str(uuid.UUID(self.binding['vmId']))
        common = "$ErrorActionPreference='Stop'; $vm=Get-VM -Id '" + ident + "'; "
        commands = {
            'status': "$vm | Select-Object Id,Name,State | ConvertTo-Json -Compress",
            'start': "if ($vm.State -eq 'Off') { Start-VM -VM $vm | Out-Null }; @{ok=$true}|ConvertTo-Json",
            'pause': "if ($vm.State -eq 'Running') { Suspend-VM -VM $vm }; if ((Get-VM -Id $vm.Id).State -notin @('Paused','Off')) { throw 'VM did not pause' }; @{ok=$true}|ConvertTo-Json",
            'resume': "if ($vm.State -eq 'Paused') { Resume-VM -VM $vm }; if ((Get-VM -Id $vm.Id).State -notin @('Running','Off')) { throw 'VM did not resume' }; @{ok=$true}|ConvertTo-Json",
            'stop': "if ($vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }; if ((Get-VM -Id $vm.Id).State -ne 'Off') { throw 'VM did not stop' }; @{ok=$true}|ConvertTo-Json",
            'checkpoint': "Set-VM -VM $vm -CheckpointType ProductionOnly; $name='permissive-healthy-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff'); try { Checkpoint-VM -VM $vm -SnapshotName $name -ErrorAction Stop } catch { Stop-VM -VM $vm -TurnOff -Force; Set-VM -VM $vm -CheckpointType Standard; Checkpoint-VM -VM $vm -SnapshotName $name; Set-VM -VM $vm -CheckpointType ProductionOnly; Start-VM -VM $vm | Out-Null }; $all=@(Get-VMSnapshot -VM $vm | Where-Object {$_.Name -like 'permissive-healthy-*'} | Sort-Object CreationTime -Descending); $all | Select-Object -Skip 3 | Remove-VMSnapshot -Confirm:$false; @{ok=$true;checkpoint=$name}|ConvertTo-Json",
            'restore': "$snapshot=Get-VMSnapshot -VM $vm | Where-Object {$_.Name -like 'permissive-healthy-*' -or $_.Name -eq 'permissive-baseline'} | Sort-Object CreationTime -Descending | Select-Object -First 1; if (!$snapshot) { throw 'No recovery checkpoint exists' }; if ($vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }; Restore-VMSnapshot -VMSnapshot $snapshot -Confirm:$false; Start-VM -VM $vm | Out-Null; @{ok=$true;checkpoint=$snapshot.Name}|ConvertTo-Json",
        }
        if operation not in commands:
            raise ValueError('Unknown environment operation')
        return await self._process('-Command', common + commands[operation])


class VMRuntime:
    role = 'controller'
    guest_paths = True
    max_concurrent_tasks = 1

    def __init__(self, home, binding_path, transport=None):
        self.home = Path(home)
        self.binding_path = Path(binding_path)
        self.binding = deployment(binding_path)
        if self.binding['role'] != 'controller':
            raise ValueError('VMRuntime requires a controller deployment')
        self.transport = transport or HyperVTransport(binding_path)
        self.state_path = self.home / 'environment.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            'environmentId': self.binding.get('sandboxId', self.binding['vmId']),
            'vmId': self.binding['vmId'], 'generation': 1, 'phase': 'ready'}
        self.workers, self.controls = {}, {}
        self.lifecycle_lock = LifecycleLock()
        self.store = None
        self.min_host_free_bytes = self.binding.get('minHostFreeBytes', 64 * 1024**3)
        self.max_disk_bytes = self.binding.get('maxDiskBytes', 160 * 1024**3)
        for value in (self.min_host_free_bytes, self.max_disk_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError('Host disk budgets must be positive integer byte counts')
        self.storage_path = Path(self.binding.get('stateDirectory', self.binding_path.parent))
        self.disk_check_interval = 2
        self._monitor_task = None
        self._closing = False
        self._closed = False
        self._capacity_resetting = False
        self._disk_stopped = asyncio.Event()
        if not self.state.get('hostDiskGuard') or self.state['hostDiskGuard'].get('stopConfirmed'):
            self._disk_stopped.set()

    def attach(self, store):
        self.store = store
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS vm_attempts (
                task_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, generation INTEGER NOT NULL,
                guest_task_id TEXT, guest_workspace_id TEXT, cursor INTEGER NOT NULL DEFAULT 0,
                phase TEXT NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS vm_event_receipts (
                attempt_id TEXT NOT NULL, guest_event_id INTEGER NOT NULL,
                PRIMARY KEY(attempt_id,guest_event_id));
            CREATE TABLE IF NOT EXISTS vm_journal_usage (
                task_id TEXT PRIMARY KEY, event_count INTEGER NOT NULL,
                journal_bytes INTEGER NOT NULL, through_event_id INTEGER NOT NULL,
                status_bytes INTEGER NOT NULL DEFAULT 0);
        ''')

    def _save(self):
        atomic_json(self.state_path, self.state)

    def _disk_capacity(self):
        checks = []
        for label, path in (('vmStorage', self.storage_path), ('controllerHome', self.home)):
            # These paths come only from the host binding, never guest output.
            try:
                existing = path.resolve()
                while not existing.exists() and existing != existing.parent:
                    existing = existing.parent
                checks.append({'volume': label, 'path': str(path), 'freeBytes': shutil.disk_usage(existing).free})
            except OSError:
                checks.append({'volume': label, 'path': str(path), 'freeBytes': None})
        return checks

    def _capacity_low(self, checks):
        return any(row['freeBytes'] is None or row['freeBytes'] < self.min_host_free_bytes for row in checks)

    def _host_event(self, kind, data):
        if self.store is not None:
            for ident in list(self.workers):
                with contextlib.suppress(Exception):
                    self.store.event(ident, kind, data)

    def _fence_capacity(self, checks):
        first = not self.state.get('hostDiskGuard')
        guard = self.state.setdefault('hostDiskGuard', {'stopConfirmed': False})
        if self._capacity_resetting:
            guard['stopConfirmed'] = False
        guard.update(minHostFreeBytes=self.min_host_free_bytes, checks=checks,
                     message='Host storage reserve reached. Guest execution is quarantined; free host space and reset the environment.')
        if self.state.get('phase') != 'exported':
            self.state['phase'] = 'quarantined'
        self.state['hostNotice'] = guard['message']
        if not guard['stopConfirmed']:
            self._disk_stopped.clear()
        # A genuinely full volume must not prevent the stop command itself.
        with contextlib.suppress(OSError):
            self._save()
        if first:
            self._host_event('host_capacity', dict(guard))
        for control in self.controls.values():
            control.set()

    async def _stop_for_capacity(self):
        guard = self.state['hostDiskGuard']
        if guard.get('stopConfirmed'):
            return
        try:
            result = await self.transport.lifecycle('stop')
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise RuntimeError('Hyper-V did not confirm shutdown')
        except Exception:
            guard['stopConfirmed'] = False
            return
        guard['stopConfirmed'] = True
        self._disk_stopped.set()
        with contextlib.suppress(OSError):
            self._save()

    async def _check_capacity(self, *, resetting=False):
        checks = self._disk_capacity()
        if self._capacity_low(checks):
            self._fence_capacity(checks)
            await self._stop_for_capacity()
            raise HostDiskCapacityError(self.state['hostNotice'])
        if self.state.get('hostDiskGuard') and not resetting:
            raise HostDiskCapacityError('Host storage quarantine requires an explicit environment reset after capacity is restored')
        return checks

    async def _lifecycle(self, operation, *, resetting=False):
        if operation in {'start', 'resume', 'restore', 'checkpoint'}:
            if self._closing:
                raise RuntimeError('Host controller is shutting down')
            checks = await self._check_capacity(resetting=resetting)
            if operation == 'checkpoint' and checks[0]['freeBytes'] < self.min_host_free_bytes + 2 * self.max_disk_bytes:
                # Merging a removed AVHDX can require a full extra disk copy
                # while the running guest grows another differencing disk.
                message = 'Checkpoint skipped: VM storage lacks the host reserve plus two maximum guest disks of merge headroom'
                self._host_event('checkpoint_error', {'message': message})
                return {'ok': False, 'skipped': True, 'message': message}
        return await self.transport.lifecycle(operation)

    async def _capacity_watch(self):
        while not self._closing:
            checks = self._disk_capacity()
            if self._capacity_low(checks):
                self._fence_capacity(checks)
            guard = self.state.get('hostDiskGuard')
            if guard and not guard.get('stopConfirmed') and (self._capacity_low(checks) or not self._capacity_resetting):
                # Startup/readiness can hold this lock for minutes. Abort that
                # fixed helper first; its cancellation closes its process tree.
                owner = self.lifecycle_lock.owner
                if owner is not None and owner is not asyncio.current_task():
                    owner.cancel()
                async with self.lifecycle_lock:
                    await self._stop_for_capacity()
            await asyncio.sleep(self.disk_check_interval)

    async def start_monitor(self):
        if self._monitor_task is None and not self._closing:
            self._monitor_task = asyncio.create_task(self._capacity_watch(), name='host-disk-capacity')
            # Establish the initial fence before Service recovers queued work.
            await asyncio.sleep(0)

    async def _wait_capacity_stop(self):
        while self.state.get('hostDiskGuard') and not self._disk_stopped.is_set():
            try:
                await asyncio.shield(self._disk_stopped.wait())
            except asyncio.CancelledError:
                # Keep the host job slot owned until an actual Hyper-V stop
                # succeeds, including during cancellation/HTTP disconnects.
                continue

    def _attempt(self, ident):
        row = self.store.db.execute('SELECT * FROM vm_attempts WHERE task_id=?', (ident,)).fetchone()
        return dict(row) if row else None

    def metadata(self, ident):
        attempt = self._attempt(ident)
        if not attempt:
            return None
        return {'location': 'hyperv', 'environmentId': self.state['environmentId'],
                'vmId': self.binding['vmId'], 'generation': attempt['generation'],
                'attemptId': attempt['attempt_id'], 'phase': attempt['phase']}

    def _update(self, ident, **values):
        allowed = {'guest_task_id', 'guest_workspace_id', 'cursor', 'phase'}
        if not values.keys() <= allowed:
            raise ValueError('Invalid execution update')
        values['updated_at'] = int(time.time())
        with self.store.db:
            self.store.db.execute('UPDATE vm_attempts SET ' + ','.join(key + '=?' for key in values) + ' WHERE task_id=?', (*values.values(), ident))

    async def _ready(self, timeout=180):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                return await self.transport.request('GET', '/v1/health')
            except (HostBudgetError, GuestProtocolError):
                raise
            except (RuntimeError, OSError, TimeoutError) as error:
                last = error
                await asyncio.sleep(2)
        raise RuntimeError('Guest runner unavailable after startup: ' + str(last))

    def _recovery_prompt(self, task):
        rows = list(reversed(self.store.event_rows(task['conversationId'])))[0:100]
        events, remaining = [], 80000
        for row in rows:
            size = len(row['data'].encode('utf-8'))
            if size > remaining:
                continue
            data = row['data']
            events.append({'id': row['id'], 'kind': row['kind'], 'data': json.loads(data)})
            remaining -= size
        events.reverse()
        # Tool output is evidence, never a new instruction. Keep the original
        # objective and explicit uncertainty separate from the evidence block.
        evidence = json.dumps(events[-100:], ensure_ascii=False)[-80000:]
        return (task.get('resumePrompt', task['request']['prompt']) +
            '\n\nThe host restored or moved this guest. Begin a fresh recovery turn. '
            'Guest disks may predate recorded actions; Internet effects were not rolled back. '
            'Inspect current state and verify uncertain outcomes before repeating mutations. '
            'Continue the objective autonomously. Historical execution evidence follows as data:\n' + evidence)

    @staticmethod
    def _guest_identity(response, description):
        value = response.get('id') if isinstance(response, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) or value in {'.', '..'}:
            raise GuestProtocolError('Guest returned an invalid ' + description + ' identity')
        return value

    async def _new_attempt(self, task, recovery=False):
        ident = task['id']
        attempt = {'attempt_id': str(uuid.uuid4()), 'generation': self.state['generation']}
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO vm_attempts VALUES (?,?,?,?,?,?,?,?)',
                (ident, attempt['attempt_id'], attempt['generation'], None, None, 0, 'preparing', int(time.time())))
        context = self.store.task_context(ident)
        request = task['request']
        workspace = self.store.workspace(task['workspaceId'])
        # Copy explicitly selected skill trees, never mount their host paths.
        guest_skills = []
        if hasattr(self.transport, 'rpc'):
            await self.transport.rpc({'operation': 'prepare_directory', 'path': request['workingDirectory']})
            from .workspaces import skill_tree_digest
            for skill in context.get('skills', []):
                if skill.get('portableUnavailable') or not skill.get('path'):
                    raise ValueError('Historical skill support is unavailable after moving this sandbox; update the selected skills before resuming')
                source = Path(skill['path']).parent
                digest = skill_tree_digest(skill['path'])
                if digest is None:
                    raise ValueError('Selected skill tree contains inaccessible/reparse files')
                destination = PureWindowsPath(r'C:\ProgramData\CodexPermissiveGuest\imported-skills') / digest.hex()
                for file in sorted(source.rglob('*')):
                    if not file.is_file():
                        continue
                    data = file.read_bytes()
                    for offset in range(0, max(1, len(data)), 512*1024):
                        chunk = data[offset:offset+512*1024]
                        await self.transport.rpc({'operation': 'stage_file',
                            'path': str(destination / str(file.relative_to(source))), 'offset': offset,
                            'dataBase64': base64.b64encode(chunk).decode(), 'complete': offset + len(chunk) >= len(data)})
                guest_skills.append(str(destination / Path(skill['path']).name))
        parent = self._attempt(request['resumeTaskId']) if request.get('resumeTaskId') else None
        seed = self.store.history_seed(ident)
        reuse_parent = parent and parent['generation'] == self.state['generation'] and parent['guest_task_id'] and not recovery and seed is None
        if request.get('resumeTaskId') and not reuse_parent and seed is None:
            # Ordinary continuations reuse native context. A replaced/reset VM
            # needs the saved pre-turn checkpoint instead of an empty thread.
            seed = self.store.task_context(ident).get('_regenerationSeed')
            if seed is None:
                raise RuntimeError('The retained conversation is not ready to restore in this guest')
            self.store.set_history_seed(ident, seed)
        workspace_body = {
            'name': workspace['name'], 'workingDirectory': request['workingDirectory'],
            'systemPrompt': context.get('systemPrompt', ''), 'skills': guest_skills,
            'browserMode': request['browserMode'], 'browser': request.get('browser', 'chromium'),
            'browserProfile': request.get('browserProfile', '')}
        guest_workspace = await self.transport.request('PATCH' if reuse_parent else 'POST',
            '/v1/workspaces' + ('/' + parent['guest_workspace_id'] if reuse_parent else ''), workspace_body)
        guest_workspace_id = self._guest_identity(guest_workspace, 'workspace')
        guest_request = {key: value for key, value in request.items() if key in {
            'prompt', 'browserMode', 'browser', 'browserProfile', 'mode', 'goal'}}
        guest_request.update(workspaceId=guest_workspace_id, workingDirectory=request['workingDirectory'],
                             submissionId=attempt['attempt_id'], credentialWorkspaceId=task['workspaceId'])
        if recovery:
            guest_request['prompt'] = self._recovery_prompt(task)
        ids = []
        from .attachments import Attachments
        attachments = Attachments(self.store)
        seed_ids = {attachment for message in seed or [] for attachment in message['attachments']}
        uploaded = {}
        for attachment in dict.fromkeys([*request.get('attachments', []), *sorted(seed_ids)]):
            row = attachments.get(attachment)
            result = await self.transport.request('POST', '/v1/attachments', {
                'workspaceId': guest_workspace_id, 'name': row['name'],
                'data': base64.b64encode(attachments.path(attachment).read_bytes()).decode()})
            uploaded[attachment] = self._guest_identity(result, 'attachment')
        ids = [uploaded[attachment] for attachment in request.get('attachments', [])]
        if ids:
            guest_request['attachments'] = ids
        if seed is not None:
            guest_request['historyImport'] = [{**message, 'attachments': [uploaded[value] for value in message['attachments']]} for message in seed]
        # A surviving predecessor can resume its actual native conversation.
        if reuse_parent:
            guest_request['resumeTaskId'] = parent['guest_task_id']
            guest_request['workspaceId'] = parent['guest_workspace_id']
        self.store.event(ident, 'execution_dispatch', {'attemptId': attempt['attempt_id'],
            'generation': attempt['generation'], 'outcome': 'unknown_until_guest_receipt'})
        try:
            result = await self.transport.request('POST', '/v1/tasks', guest_request)
        except (HostBudgetError, GuestProtocolError):
            raise
        except (RuntimeError, OSError, TimeoutError):
            # Admission UUID, not a native tool call, can be retried safely.
            result = await self.transport.request('POST', '/v1/tasks', guest_request)
        self._update(ident, guest_task_id=self._guest_identity(result, 'task'), guest_workspace_id=guest_request['workspaceId'], phase='running')
        self.store.event(ident, 'execution', self.metadata(ident))
        return self._attempt(ident)

    async def erase_history(self, conversation, store):
        """Delete guest copies before replacing a host conversation prefix."""
        from .secure_delete import checked
        rows = store.db.execute('SELECT a.guest_task_id FROM vm_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.conversation_id=? AND a.guest_task_id IS NOT NULL',
                                (conversation,)).fetchall()
        uuid.UUID(conversation)
        journal = checked(store.home / 'guest-history-deletions' / (conversation + '.json'), store.home)
        targets = set(json.loads(journal.read_text(encoding='utf-8'))['conversations']) if journal.exists() else set()
        if not rows and not targets:
            return
        if self.state.get('phase') in {'exported', 'quarantined'}:
            raise RuntimeError('Restore or resume the guest before deleting its saved history')
        async with self.lifecycle_lock:
            await self._lifecycle('resume')
            await self._lifecycle('start')
            await self._erase_history_copies(rows, journal, targets)

    async def _erase_history_copies(self, rows, journal, targets):
        from .history import save_edit
        await self._ready()
        # List first so retrying a partially completed deletion also succeeds.
        remaining, cursor = {}, None
        while True:
            page = await self.transport.request('GET', '/v1/tasks' + ('?cursor=' + str(cursor) if cursor else ''))
            for task in page['data']:
                remaining[task['id']] = task['conversationId']
            cursor = page.get('nextCursor')
            if not cursor:
                break
        targets.update(remaining[row['guest_task_id']] for row in rows if row['guest_task_id'] in remaining)
        if not targets:
            return
        for ident in targets:
            self._guest_identity({'id': ident}, 'conversation')
        journal.parent.mkdir(exist_ok=True)
        save_edit(journal, {'conversations': sorted(targets)})
        for ident in sorted(targets):
            try:
                await self.transport.request('POST', '/v1/chats/' + ident + '/secure-delete', {'confirm': ident, 'passes': 7})
            except RuntimeError as error:
                # An earlier confirmed erase may have completed before its reply
                # was lost. Other errors must retain the cleanup journal.
                if not str(error).startswith('guest_api_error: Guest API HTTP 404:'):
                    raise
        journal.unlink()

    def _journal_totals(self, ident):
        row = self.store.db.execute('SELECT * FROM vm_journal_usage WHERE task_id=?', (ident,)).fetchone()
        usage = dict(row) if row else {'task_id': ident, 'event_count': 0, 'journal_bytes': 0, 'through_event_id': 0, 'status_bytes': 0}
        # Include host-authored rows as well. The high-water mark avoids rescanning
        # the complete journal on every poll and survives attempts and restarts.
        delta = self.store.db.execute('SELECT count(*) AS count, coalesce(sum(length(CAST(e.kind AS BLOB))+coalesce(s.bytes,length(CAST(e.data AS BLOB)))+256),0) AS bytes, coalesce(max(e.id),0) AS last FROM events e LEFT JOIN event_payload_sizes s ON s.event_id=e.id WHERE task_id=? AND e.id>?', (ident, usage['through_event_id'])).fetchone()
        usage['event_count'] += delta['count']
        usage['journal_bytes'] += delta['bytes']
        usage['through_event_id'] = max(usage['through_event_id'], delta['last'])
        return usage

    def _save_journal_totals(self, usage):
        self.store.db.execute('INSERT OR REPLACE INTO vm_journal_usage VALUES (?,?,?,?,?)',
            tuple(usage[key] for key in ('task_id', 'event_count', 'journal_bytes', 'through_event_id', 'status_bytes')))

    def _check_journal_budget(self, usage):
        if usage['event_count'] > int(self.binding.get('maxEvents', 100000)) or usage['journal_bytes'] + usage['status_bytes'] > int(self.binding.get('maxJournalBytes', 256*1024**2)):
            raise HostBudgetError('Guest output exceeded the host task journal budget')

    def _validate_guest_status(self, ident, attempt, guest):
        if not isinstance(guest, dict) or guest.get('id') != attempt['guest_task_id'] or not isinstance(guest.get('status'), str) or guest['status'] not in TERMINAL | {'queued', 'running', 'pausing', 'paused', 'cancelling'}:
            raise GuestProtocolError('Guest returned an invalid task status')
        fields = {key: guest.get(key) for key in ('result', 'error', 'threadId')}
        if any(value is not None and not isinstance(value, str) for value in fields.values()):
            raise GuestProtocolError('Guest task result, error and thread identity must be text')
        thread = fields['threadId']
        if thread is not None and (not thread or len(thread) > 256 or any(ord(char) < 32 for char in thread)):
            raise GuestProtocolError('Guest returned an invalid native thread identity')
        try:
            size = len(json.dumps(fields, ensure_ascii=False).encode('utf-8'))
        except UnicodeError as error:
            raise GuestProtocolError('Guest returned invalid Unicode task output') from error
        if size > int(self.binding.get('maxStatusBytes', 8*1024**2)):
            raise HostBudgetError('Guest task status exceeded the host output budget')
        with self.store.db:
            usage = self._journal_totals(ident)
            usage['status_bytes'] = size
            self._check_journal_budget(usage)
            self._save_journal_totals(usage)

    def ingest(self, ident, attempt, events):
        current = self._attempt(ident)
        if not current or current['attempt_id'] != attempt['attempt_id'] or current['generation'] != self.state['generation']:
            return 0
        if not isinstance(events, list) or len(events) > 200:
            raise GuestProtocolError('Guest returned an invalid event batch')
        prepared, previous = [], 0
        for event in events:
            if not isinstance(event, dict) or type(event.get('id')) is not int or not 0 < event['id'] <= 2**63-1 or event['id'] <= previous:
                raise GuestProtocolError('Guest returned invalid or unordered event identities')
            previous = event['id']
            kind, data = event.get('kind'), event.get('data')
            if 'data' not in event or not isinstance(kind, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:/-]{0,63}', kind):
                raise GuestProtocolError('Guest returned an invalid event shape')
            if kind in {'agent', 'usage'} and not isinstance(data, dict):
                raise GuestProtocolError('Guest returned an invalid agent or usage event')
            if kind == 'agent' and data.get('method') is not None and not isinstance(data['method'], str):
                raise GuestProtocolError('Guest returned an invalid agent method')
            active_turn = None
            if kind == 'agent' and data.get('method') in {'turn/started', 'turn/completed'}:
                params = data.get('params', {})
                turn = params.get('turn', {}) if isinstance(params, dict) else None
                if not isinstance(turn, dict) or turn.get('id') is not None and (not isinstance(turn['id'], str) or len(turn['id']) > 256):
                    raise GuestProtocolError('Guest returned an invalid turn identity')
                active_turn = turn.get('id') if data['method'] == 'turn/started' else None
            created = event.get('createdAt', time.time())
            if type(created) not in {int, float} or not 0 <= created <= 253402300799:
                raise GuestProtocolError('Guest returned an invalid event timestamp')
            try:
                encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
                size = len(encoded.encode('utf-8')) + len(kind.encode('utf-8')) + 256
            except (TypeError, ValueError, RecursionError, UnicodeError) as error:
                raise GuestProtocolError('Guest event is not valid JSON data') from error
            if size > int(self.binding.get('maxEventBytes', 8*1024**2)):
                raise HostBudgetError('Guest event exceeded the host event budget')
            prepared.append((event['id'], kind, data, encoded, created, active_turn, size))
        accepted = 0
        with self.store.db:
            usage = self._journal_totals(ident)
            for cursor, kind, data, encoded, created, active_turn, size in prepared:
                if cursor <= current['cursor'] and not self.store.db.execute('SELECT 1 FROM vm_event_receipts WHERE attempt_id=? AND guest_event_id=?', (attempt['attempt_id'], cursor)).fetchone():
                    raise GuestProtocolError('Guest event cursor regressed')
                receipt = self.store.db.execute('INSERT OR IGNORE INTO vm_event_receipts VALUES (?,?)', (attempt['attempt_id'], cursor))
                if receipt.rowcount:
                    usage['event_count'] += 1
                    usage['journal_bytes'] += size
                    self._check_journal_budget(usage)
                    inserted = self.store.db.execute('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)',
                        (ident, kind, encoded, created))
                    usage['through_event_id'] = inserted.lastrowid
                    if kind == 'usage':
                        self.store.db.execute('UPDATE tasks SET token_usage=? WHERE id=?', (encoded, ident))
                    if kind == 'agent' and data.get('method') in {'turn/started', 'turn/completed'}:
                        self.store.db.execute('UPDATE tasks SET active_turn_id=? WHERE id=?', (active_turn, ident))
                    accepted += 1
                self.store.db.execute('UPDATE vm_attempts SET cursor=max(cursor,?) WHERE task_id=?', (cursor, ident))
            self._save_journal_totals(usage)
        with self.store.db:
            self.store.segments.sync_pending(self.store.get(ident)['conversationId'])
        return accepted

    def _ingest_page(self, ident, attempt, page):
        current = self._attempt(ident)
        if not current or current['attempt_id'] != attempt['attempt_id'] or current['generation'] != self.state['generation']:
            return 0
        if not isinstance(page, dict) or type(page.get('hasMore')) is not bool or type(page.get('nextCursor')) is not int:
            raise GuestProtocolError('Guest returned an invalid event page')
        events = page.get('data')
        if not isinstance(events, list) or len(events) > 200:
            raise GuestProtocolError('Guest returned an invalid event batch')
        # Validate the advertised cursor before committing any batch records.
        final = events[-1].get('id') if events and isinstance(events[-1], dict) else current['cursor']
        if type(final) is not int or page['nextCursor'] != max(current['cursor'], final) or page['hasMore'] and page['nextCursor'] <= current['cursor']:
            raise GuestProtocolError('Guest event page made no cursor progress')
        return self.ingest(ident, attempt, events)

    @staticmethod
    def _artifact_name(name):
        reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {prefix + number for prefix in ('COM', 'LPT') for number in '123456789¹²³'}
        if not isinstance(name, str) or not name or len(name) > 128 or name in {'.', '..'} or name[-1] in ' .' or PureWindowsPath(name).name != name or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name) or name.split('.')[0].rstrip(' ').upper() in reserved:
            raise GuestProtocolError('Guest returned an invalid Windows artifact identity')
        try:
            if len(name.encode('utf-16-le')) > 256:
                raise GuestProtocolError('Guest artifact identity exceeds the Windows filename budget')
        except UnicodeError as error:
            raise GuestProtocolError('Guest returned an invalid Unicode artifact identity') from error
        return name

    def _artifact_root(self, ident):
        self._artifact_name(ident)
        base = self.home / 'artifacts'
        root = base / ident
        for path in (base, root):
            try:
                info = path.lstat()
            except FileNotFoundError:
                path.mkdir()
                info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise GuestProtocolError('Host artifact directory is not a regular directory')
        if base.resolve().parent != self.home.resolve() or root.resolve().parent != base.resolve():
            raise GuestProtocolError('Host artifact directory escaped its storage root')
        return root

    async def _artifacts(self, ident, guest, attempt=None):
        maximum = int(self.binding.get('maxArtifactBytes', 2*1024**3))
        task_maximum = int(self.binding.get('maxTaskArtifactBytes', 8*1024**3))
        count_maximum = int(self.binding.get('maxArtifacts', 256))
        inventory = guest.get('artifacts', [])
        if not isinstance(inventory, list) or len(inventory) > count_maximum:
            raise HostBudgetError('Guest artifact inventory exceeded the host export budget')
        prepared, names = [], set()
        for item in inventory:
            if not isinstance(item, dict):
                raise GuestProtocolError('Guest returned invalid artifact metadata')
            name = self._artifact_name(item.get('id'))
            if name.casefold() in names:
                raise GuestProtocolError('Guest returned duplicate Windows artifact identities')
            names.add(name.casefold())
            expected = item.get('size')
            if type(expected) is not int or expected < 0:
                raise GuestProtocolError('Guest artifact has no valid size')
            prepared.append((name, expected))
        # Retained files from earlier interrupted exports count toward this task's
        # quota too. A replacement reserves its temporary bytes before writing.
        root = self._artifact_root(ident)
        existing = {}
        for path in root.iterdir():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise GuestProtocolError('Host artifact storage contains a nonregular entry')
            existing[path.name.casefold()] = info.st_size
        retained = sum(existing.values())
        if retained > task_maximum or len(existing) > count_maximum:
            raise HostBudgetError('Retained artifacts exceeded the host task export budget')
        for name, expected in prepared:
            if expected > min(maximum, task_maximum - retained) or name.casefold() not in existing and len(existing) >= count_maximum:
                self.store.event(ident, 'artifact_omitted', {'id': name, 'size': expected, 'reason': 'host_export_budget'})
                continue
            temporary = root / ('.' + uuid.uuid4().hex)
            offset = 0
            try:
                with temporary.open('xb') as output:
                    while True:
                        page = await self.transport.request('GET', '/v1/tasks/' + guest['id'] + '/artifacts/' + name, offset=offset, length=512*1024)
                        if attempt is not None and (attempt['generation'] != self.state['generation'] or self._attempt(ident)['attempt_id'] != attempt['attempt_id']):
                            return False
                        if not isinstance(page, dict) or not isinstance(page.get('data'), str) or len(page['data']) > ((512*1024 + 2)//3)*4:
                            raise GuestProtocolError('Guest artifact returned an oversized or invalid chunk')
                        try:
                            data = base64.b64decode(page['data'], validate=True)
                        except (ValueError, UnicodeError) as error:
                            raise GuestProtocolError('Guest artifact returned invalid base64') from error
                        if any(type(page.get(key)) is not int for key in ('offset', 'length', 'size')) or page['offset'] != offset or page['length'] != len(data) or page['size'] != expected or offset + len(data) > expected or type(page.get('eof')) is not bool:
                            raise GuestProtocolError('Guest artifact changed or returned an invalid chunk')
                        output.write(data)
                        if page['eof']:
                            if offset + len(data) != expected or page.get('nextOffset') is not None:
                                raise GuestProtocolError('Guest artifact ended before its declared size')
                            break
                        next_offset = page.get('nextOffset')
                        if type(next_offset) is not int or next_offset != offset + len(data) or next_offset <= offset:
                            raise GuestProtocolError('Guest artifact transfer made no progress')
                        offset = next_offset
                self._artifact_root(ident)
                target = root / name
                if target.exists() or target.is_symlink():
                    info = target.lstat()
                    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise GuestProtocolError('Host artifact destination is not a regular file')
                os.replace(temporary, root / name)
                retained += expected - existing.get(name.casefold(), 0)
                existing[name.casefold()] = expected
            finally:
                temporary.unlink(missing_ok=True)
        return True

    async def _reject_guest_output(self, ident):
        # Persist the fence first: even a failed Hyper-V command must prevent the
        # next task from sharing an uncontrolled, still-running guest.
        async with self.lifecycle_lock:
            if self.state.get('hostDiskGuard'):
                self._update(ident, phase='failed')
                await self._stop_for_capacity()
                return
            if self.state.get('phase') != 'exported':
                self.state['phase'] = 'quarantined'
            self._save()
            self._update(ident, phase='failed')
            try:
                await self.transport.lifecycle('stop')
                self.state['generation'] += 1
                self._save()
                # Export may fence execution while an awaited RPC is failing.
                # Confirm shutdown but never restore/start or release that fence.
                if self.state.get('phase') == 'exported':
                    return
                await self._lifecycle('restore')
                await self.transport.lifecycle('stop')
            except Exception:
                return
            if self.state.get('phase') != 'exported' and not self.state.get('hostDiskGuard'):
                self.state['phase'] = 'ready'
            self._save()

    async def run(self, task, store):
        if self.store is None:
            self.attach(store)
        if self.state.get('phase') in {'exported', 'quarantined'}:
            raise RuntimeError('Environment execution is fenced; restore a quarantined guest before starting another task')
        ident = task['id']
        self.workers[ident] = True
        self.controls[ident] = asyncio.Event()
        self.controls[ident].set()
        guest_may_be_active = False
        try:
            # The same lifecycle lock fences admission and operator controls:
            # no startup/checkpoint command can restart an already-paused VM.
            async with self.lifecycle_lock:
                if self.state.get('phase') in {'exported', 'quarantined'}:
                    raise RuntimeError('Source execution is fenced for migration')
                guest_may_be_active = True
                await self._lifecycle('resume')
                await self._lifecycle('start')
                await self._ready()
                attempt = self._attempt(ident)
                if task.get('streamRecoveryRequested'):
                    if not attempt or attempt['generation'] != self.state['generation'] or not attempt['guest_task_id']:
                        raise RuntimeError('The original guest conversation is unavailable for response recovery')
                    retried = await self.transport.request('POST', '/v1/tasks/' + attempt['guest_task_id'] + '/retry')
                    self._validate_guest_status(ident, attempt, retried)
                    self._update(ident, phase='running')
                if not attempt or attempt['phase'] in {'completed', 'cancelled'}:
                    await self._lifecycle('checkpoint')
                    await self._ready()
                    if self.store.get(ident)['status'] not in {'pausing', 'paused', 'cancelling'}:
                        attempt = await self._new_attempt(task, recovery=bool(task.get('resumePrompt')))
                elif attempt['generation'] != self.state['generation'] or not attempt['guest_task_id']:
                    if self.store.get(ident)['status'] not in {'pausing', 'paused', 'cancelling'}:
                        attempt = await self._new_attempt(task, recovery=True)
            failures = 0
            while True:
                await self.controls[ident].wait()
                if self.state.get('hostDiskGuard'):
                    raise HostDiskCapacityError(self.state['hostDiskGuard']['message'])
                if self.state.get('phase') == 'exported':
                    await asyncio.sleep(1)
                    continue
                if self.store.get(ident)['status'] == 'cancelling':
                    return
                attempt = self._attempt(ident)
                if not attempt or attempt['generation'] != self.state['generation'] or not attempt['guest_task_id']:
                    async with self.lifecycle_lock:
                        if self.store.get(ident)['status'] in {'paused', 'pausing', 'cancelling'}:
                            continue
                        await self._ready()
                        attempt = await self._new_attempt(task, recovery=True)
                try:
                    page = await self.transport.request('GET', '/v1/tasks/' + attempt['guest_task_id'] + '/events', after=attempt['cursor'], limit=200)
                    self._ingest_page(ident, attempt, page)
                    guest = await self.transport.request('GET', '/v1/tasks/' + attempt['guest_task_id'])
                    if attempt['generation'] != self.state['generation']:
                        continue
                    self._validate_guest_status(ident, attempt, guest)
                    failures = 0
                except (HostBudgetError, GuestProtocolError):
                    raise
                except (RuntimeError, OSError, TimeoutError):
                    failures += 1
                    # Quiet inference is not a failure: only failed guest RPCs
                    # reach this path. The host can recover without guest help.
                    if failures < 3:
                        await asyncio.sleep(2)
                        continue
                    async with self.lifecycle_lock:
                        if self.store.get(ident)['status'] in {'pausing', 'paused', 'cancelling'}:
                            continue
                        if hasattr(self.transport, 'rpc'):
                            with contextlib.suppress(Exception):
                                await self.transport.rpc({'operation': 'runner_restart'})
                                await self._ready(timeout=30)
                                # Query the old admission before any rollback.
                                await self.transport.request('GET', '/v1/tasks/' + attempt['guest_task_id'])
                                self.state['phase'] = 'ready'
                                self._save()
                                self._update(ident, phase='running')
                                failures = 0
                                continue
                        self.state.update(generation=self.state['generation'] + 1, phase='recovering')
                        self._save()
                        self._update(ident, phase='recovering')
                        self.store.event(ident, 'execution', self.metadata(ident))
                        await self._lifecycle('restore')
                        await self._ready()
                        attempt = await self._new_attempt(task, recovery=True)
                        self.state['phase'] = 'ready'
                        self._save()
                    failures = 0
                    continue
                if self.state.get('hostDiskGuard'):
                    raise HostDiskCapacityError(self.state['hostDiskGuard']['message'])
                if guest.get('threadId'):
                    store.update(ident, thread_id=guest['threadId'])
                    store.set_history_seed(ident, None)
                if guest['status'] in TERMINAL:
                    # Drain the final page before publishing completion.
                    pages = 0
                    while page.get('hasMore'):
                        pages += 1
                        if pages > int(self.binding.get('maxEventDrainPages', 1000)):
                            raise HostBudgetError('Guest terminal event drain exceeded the host page budget')
                        await self.controls[ident].wait()
                        if self.state.get('hostDiskGuard'):
                            raise HostDiskCapacityError(self.state['hostDiskGuard']['message'])
                        if self.store.get(ident)['status'] == 'cancelling':
                            return
                        if attempt['generation'] != self.state['generation']:
                            break
                        page = await self.transport.request('GET', '/v1/tasks/' + attempt['guest_task_id'] + '/events', after=self._attempt(ident)['cursor'], limit=200)
                        self._ingest_page(ident, attempt, page)
                    if attempt['generation'] != self.state['generation']:
                        continue
                    await self._artifacts(ident, guest, attempt)
                    if attempt['generation'] != self.state['generation']:
                        continue
                    self._update(ident, phase=guest['status'])
                    store.update(ident, status=guest['status'], result=guest.get('result'), error=guest.get('error'))
                    if guest['status'] == 'completed':
                        try:
                            async with self.lifecycle_lock:
                                if self.state.get('phase') != 'exported' and attempt['generation'] == self.state['generation']:
                                    await self._lifecycle('checkpoint')
                        except Exception as error:
                            store.event(ident, 'checkpoint_error', {'message': str(error)})
                    return
                await asyncio.sleep(1)
        except Exception:
            # A lost admission receipt is ambiguous: the guest may already be
            # executing even though no guest task ID reached the host. Hold the
            # task slot until shutdown/recovery is confirmed or a fence persists.
            if guest_may_be_active:
                await self._reject_guest_output(ident)
            raise
        finally:
            if self.state.get('hostDiskGuard') and not self._disk_stopped.is_set():
                await self.start_monitor()
                await self._wait_capacity_stop()
            self.workers.pop(ident, None)
            self.controls.pop(ident, None)

    async def pause(self, ident, store):
        async with self.lifecycle_lock:
            self.controls[ident].clear()
            attempt = self._attempt(ident)
            if attempt and attempt['guest_task_id']:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.transport.request('POST', '/v1/tasks/' + attempt['guest_task_id'] + '/pause'), 5)
            await self.transport.lifecycle('pause')
            store.update(ident, status='paused')
            self._update(ident, phase='paused')

    async def resume(self, ident, store):
        async with self.lifecycle_lock:
            if self.state.get('phase') in {'exported', 'quarantined'}:
                raise RuntimeError('Source execution is fenced for migration')
            await self._lifecycle('resume')
            attempt = self._attempt(ident)
            if attempt and attempt['guest_task_id']:
                with contextlib.suppress(Exception):
                    await self.transport.request('POST', '/v1/tasks/' + attempt['guest_task_id'] + '/resume')
            self._update(ident, phase='running')
            self.controls[ident].set()

    async def interrupt(self, ident, store):
        async with self.lifecycle_lock:
            if ident not in self.workers:
                return
            self.controls[ident].clear()
            await self.transport.lifecycle('stop')
            if self.state.get('hostDiskGuard'):
                await self._stop_for_capacity()
                return
            self.state.update(generation=self.state['generation'] + 1, phase='recovering')
            self._save()
            self._update(ident, phase='cancelled')
            await self._lifecycle('restore')
            await self.transport.lifecycle('stop')
            self.state['phase'] = 'ready'
            self._save()

    async def steer(self, ident, store, body):
        attempt = self._attempt(ident)
        if not attempt or not attempt['guest_task_id']:
            raise ValueError('No guest turn is active')
        return await self.transport.request('POST', '/v1/tasks/' + attempt['guest_task_id'] + '/steer', body)

    async def environment(self, operation='status'):
        if self.state.get('phase') == 'exported' and operation not in {'status', 'export', 'pause'}:
            raise RuntimeError('Source execution is fenced for migration')
        if self.state.get('phase') == 'quarantined' and operation not in {'status', 'restore', 'pause', 'export'}:
            raise RuntimeError('Guest execution is quarantined; restore it before resuming')
        if operation == 'export':
            self.state['phase'] = 'exported'
            self._save()
            for ident in list(self.workers):
                await self.pause(ident, self.store)
            if not self.workers:
                async with self.lifecycle_lock:
                    await self.transport.lifecycle('pause')
            return {**self.state, 'fenced': True}
        if operation in {'pause', 'resume'} and self.workers:
            for ident in list(self.workers):
                await (self.pause(ident, self.store) if operation == 'pause' else self.resume(ident, self.store))
            return {**self.state, 'hypervisor': await self.transport.lifecycle('status')}
        async with self.lifecycle_lock:
            if operation == 'restore':
                await self._check_capacity(resetting=True)
                self.state['phase'] = 'quarantined'
                self.state['generation'] += 1
                self._save()
                await self.transport.lifecycle('stop')
                self._capacity_resetting = True
                if self.state.get('hostDiskGuard'):
                    self.state['hostDiskGuard']['stopConfirmed'] = False
                    self._disk_stopped.clear()
                    self._save()
            try:
                result = await self._lifecycle(operation, resetting=operation == 'restore')
                if operation == 'restore':
                    await self.transport.lifecycle('stop')
                    self.state.pop('hostDiskGuard', None)
                    self.state.pop('hostNotice', None)
                    self._disk_stopped.set()
                    self.state['phase'] = 'ready'
                    self._save()
            finally:
                self._capacity_resetting = False
            return {**self.state, 'hypervisor': result}

    async def close(self):
        if self._closed:
            return
        self._closing = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        owner = self.lifecycle_lock.owner
        if owner is not None and owner is not asyncio.current_task():
            owner.cancel()
        async with self.lifecycle_lock:
            if self.state.get('hostDiskGuard'):
                while not self._disk_stopped.is_set():
                    await self._stop_for_capacity()
                    if not self._disk_stopped.is_set():
                        await asyncio.sleep(self.disk_check_interval)
            else:
                try:
                    await self.transport.lifecycle('pause')
                except Exception:
                    await self.transport.lifecycle('stop')
        self._closed = True
