"""Host storage boundaries use fake disk usage and transports; never a real VM."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from permissive.store import Store
from permissive.vm_runtime import HostDiskCapacityError, VMRuntime
from test_vm_runtime import GuestTransport


class HostDiskGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / 'controller'
        self.storage = self.root / 'vm-storage'
        self.home.mkdir()
        self.storage.mkdir()
        self.binding = self.root / 'deployment.json'
        self.binding.write_text(json.dumps({
            'role': 'controller', 'vmId': str(uuid.uuid4()),
            'guestCredentialPath': 'host-owned.dpapi', 'stateDirectory': str(self.storage),
            'minHostFreeBytes': 100, 'maxDiskBytes': 200}))
        self.free = {'controller': 10000, 'vm-storage': 10000}
        self.usage = patch('permissive.vm_runtime.shutil.disk_usage', side_effect=self.disk_usage)
        self.usage.start()
        self.addCleanup(self.usage.stop)
        self.transport = GuestTransport()
        self.runtime = VMRuntime(self.home, self.binding, self.transport)
        self.runtime.disk_check_interval = .01
        self.store = Store(self.home, guest_paths=True)
        self.runtime.attach(self.store)
        self.workspace = self.store.save_workspace({
            'name': 'Guest', 'workingDirectory': r'C:\GuestWork', 'skills': []})

    def disk_usage(self, path):
        value = self.free[Path(path).name]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(free=value)

    async def asyncTearDown(self):
        self.transport.fail_lifecycle.clear()
        await self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    def task(self):
        return self.store.submit({
            'workspaceId': self.workspace['id'], 'workingDirectory': r'C:\GuestWork',
            'browserMode': 'dedicated', 'browser': 'chromium', 'prompt': 'Guest work'})[0]

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 2)

    async def test_defaults_reserve_64_gib_and_two_160_gib_disks_for_checkpoint(self):
        binding = json.loads(self.binding.read_text())
        binding.pop('minHostFreeBytes')
        binding.pop('maxDiskBytes')
        self.binding.write_text(json.dumps(binding))
        runtime = VMRuntime(self.home, self.binding, self.transport)
        self.assertEqual(runtime.min_host_free_bytes, 64 * 1024**3)
        self.assertEqual(runtime.max_disk_bytes, 160 * 1024**3)

    async def test_both_fixed_host_volumes_are_checked_before_start(self):
        self.free['controller'] = 99
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime._lifecycle('start')
        self.assertEqual(self.transport.lifecycle_calls, ['stop'])
        state = json.loads(self.runtime.state_path.read_text())
        self.assertEqual(state['phase'], 'quarantined')
        self.assertTrue(state['hostDiskGuard']['stopConfirmed'])
        self.assertEqual({row['volume'] for row in state['hostDiskGuard']['checks']}, {'controllerHome', 'vmStorage'})
        self.assertIn('free host space', state['hostNotice'])

    async def test_unreadable_disk_capacity_fails_closed(self):
        self.free['vm-storage'] = OSError('volume unavailable')
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime._lifecycle('resume')
        self.assertEqual(self.transport.lifecycle_calls, ['stop'])

    async def test_checkpoint_low_minimum_stops_without_checkpoint_or_restore(self):
        self.free['vm-storage'] = 99
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime._lifecycle('checkpoint')
        self.assertEqual(self.transport.lifecycle_calls, ['stop'])

    async def test_checkpoint_merge_headroom_shortage_only_skips_checkpoint(self):
        self.free['vm-storage'] = 499
        self.runtime._host_event = Mock()
        result = await self.runtime._lifecycle('checkpoint')
        self.assertTrue(result['skipped'])
        self.assertEqual(self.transport.lifecycle_calls, [])
        self.assertEqual(self.runtime.state['phase'], 'ready')
        self.runtime._host_event.assert_called_once()
        self.assertEqual(self.runtime._host_event.call_args.args[0], 'checkpoint_error')
        self.free['vm-storage'] = 500
        await self.runtime._lifecycle('checkpoint')
        self.assertEqual(self.transport.lifecycle_calls, ['checkpoint'])

    async def test_guest_work_can_complete_when_checkpoint_headroom_is_missing(self):
        self.free['vm-storage'] = 499
        task = self.task()
        await self.runtime.run(task, self.store)
        self.assertEqual(self.store.get(task['id'])['status'], 'completed')
        self.assertNotIn('checkpoint', self.transport.lifecycle_calls)
        self.assertEqual(self.runtime.state['phase'], 'ready')
        events = self.store.db.execute("SELECT count(*) FROM events WHERE kind='checkpoint_error'").fetchone()[0]
        self.assertEqual(events, 2)

    async def test_recovered_space_does_not_automatically_release_quarantine(self):
        self.free['vm-storage'] = 99
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime._lifecycle('start')
        self.free['vm-storage'] = 10000
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime._lifecycle('start')
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            await self.runtime.environment('resume')
        await self.runtime.environment('restore')
        self.assertEqual(self.transport.lifecycle_calls[-3:], ['stop', 'restore', 'stop'])
        self.assertEqual(self.runtime.state['phase'], 'ready')
        self.assertNotIn('hostDiskGuard', self.runtime.state)
        await self.runtime._lifecycle('start')
        self.assertEqual(self.transport.lifecycle_calls[-1], 'start')

    async def test_explicit_reset_cannot_restore_while_capacity_is_low(self):
        self.free['controller'] = 99
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime.environment('restore')
        self.assertEqual(self.transport.lifecycle_calls, ['stop'])
        self.assertNotIn('restore', self.transport.lifecycle_calls)

    async def test_idle_monitor_quarantines_and_stops_guest(self):
        await self.runtime.start_monitor()
        self.assertEqual(self.transport.lifecycle_calls, [])
        self.free['controller'] = 99
        await self.until(lambda: self.runtime.state.get('hostDiskGuard', {}).get('stopConfirmed'))
        self.assertEqual(self.runtime.state['phase'], 'quarantined')
        self.assertEqual(self.transport.lifecycle_calls, ['stop'])
        self.free['controller'] = 10000
        await asyncio.sleep(.03)
        self.assertEqual(self.runtime.state['phase'], 'quarantined')

    async def test_monitor_keeps_retrying_failed_stop_after_quarantine(self):
        self.transport.fail_lifecycle.add('stop')
        self.free['vm-storage'] = 99
        await self.runtime.start_monitor()
        await self.until(lambda: self.transport.lifecycle_calls.count('stop') >= 2)
        self.assertEqual(self.runtime.state['phase'], 'quarantined')
        self.assertFalse(self.runtime.state['hostDiskGuard']['stopConfirmed'])
        self.transport.fail_lifecycle.clear()
        await self.until(self.runtime._disk_stopped.is_set)
        self.assertTrue(self.runtime.state['hostDiskGuard']['stopConfirmed'])

    async def test_monitor_aborts_blocked_startup_but_holds_worker_until_stop_confirmed(self):
        entered = asyncio.Event()
        original = self.transport.lifecycle
        async def lifecycle(operation):
            if operation == 'resume':
                entered.set()
                await asyncio.Event().wait()
            return await original(operation)
        self.transport.lifecycle = lifecycle
        await self.runtime.start_monitor()
        task = self.task()
        job = asyncio.create_task(self.runtime.run(task, self.store))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.transport.fail_lifecycle.add('stop')
            self.free['vm-storage'] = 99
            await self.until(lambda: self.transport.lifecycle_calls.count('stop') >= 2)
            self.assertFalse(job.done())
            self.assertIn(task['id'], self.runtime.workers)
            self.transport.fail_lifecycle.clear()
            await self.until(job.done)
            with self.assertRaises(asyncio.CancelledError):
                await job
            self.assertNotIn(task['id'], self.runtime.workers)
            self.assertTrue(self.runtime._disk_stopped.is_set())
            self.assertNotIn('restore', self.transport.lifecycle_calls)
        finally:
            self.transport.fail_lifecycle.clear()
            if not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def test_unconfirmed_persisted_quarantine_retries_stop_even_after_space_recovers(self):
        self.free['vm-storage'] = 99
        self.transport.fail_lifecycle.add('stop')
        with self.assertRaises(HostDiskCapacityError):
            await self.runtime._lifecycle('start')
        self.free['vm-storage'] = 10000
        self.transport.fail_lifecycle.clear()
        self.runtime = VMRuntime(self.home, self.binding, self.transport)
        self.runtime.disk_check_interval = .01
        self.runtime.attach(self.store)
        await self.runtime.start_monitor()
        await self.until(self.runtime._disk_stopped.is_set)
        self.assertEqual(self.runtime.state['phase'], 'quarantined')
        self.assertEqual(self.transport.lifecycle_calls, ['stop', 'stop'])

    async def test_close_cancels_monitor_and_pauses_guest_even_without_jobs(self):
        await self.runtime.start_monitor()
        monitor = self.runtime._monitor_task
        await self.runtime.close()
        self.assertTrue(monitor.done())
        self.assertEqual(self.transport.lifecycle_calls, ['pause'])
        await self.runtime.close()
        self.assertEqual(self.transport.lifecycle_calls, ['pause'])

    async def test_close_falls_back_to_confirmed_stop_if_pause_fails(self):
        self.transport.fail_lifecycle.add('pause')
        await self.runtime.close()
        self.assertEqual(self.transport.lifecycle_calls, ['pause', 'stop'])

    async def test_low_capacity_does_not_enter_protocol_recovery_restore(self):
        task = self.task()
        await self.runtime._new_attempt(task)
        self.free['vm-storage'] = 99
        self.runtime._fence_capacity(self.runtime._disk_capacity())
        await self.runtime._reject_guest_output(task['id'])
        self.assertEqual(self.transport.lifecycle_calls, ['stop'])
        self.assertEqual(self.runtime.state['phase'], 'quarantined')


if __name__ == '__main__':
    unittest.main()
