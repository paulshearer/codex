import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Script, createContext } from 'node:vm';

function bridgeContext(invoke) {
  const events = new Map();
  const context = createContext({ window: {}, queueMicrotask, console: {}, __testTauri: {
    invoke, listen: async (name, callback) => { events.set(name, callback); return () => events.delete(name); },
  } });
  const source = readFileSync(new URL('../desktop/bridge.mjs', import.meta.url), 'utf8')
    .replace("import { invoke } from '@tauri-apps/api/core';", 'const { invoke } = __testTauri;')
    .replace("import { listen } from '@tauri-apps/api/event';", 'const { listen } = __testTauri;');
  new Script(source).runInContext(context);
  return { context, events, api: context.window.permissive };
}

test('Rust bridge silences content diagnostics and exposes explicit desktop operations', async () => {
  const requests = [];
  const { context, events, api } = bridgeContext(async (command, input) => { requests.push({ command, input }); return { ok: true }; });
  const hostile = { toString() { throw new Error('Sensitive object must not be formatted'); } };
  for (const method of Object.keys(context.console)) context.console[method]('PRIVATE-PROMPT', hostile);
  assert.equal(typeof api.execute, 'undefined');
  assert.equal(typeof api.sign, 'undefined');
  assert.equal(typeof api.fetch, 'undefined');
  assert.ok(Object.isFrozen(api));
  await api.submit({ prompt: 'PRIVATE-PROMPT' });
  assert.equal(requests[0].command, 'desktop_call');
  assert.equal(requests[0].input.operation, 'submit');
  assert.equal(requests[0].input.args[0].prompt, 'PRIVATE-PROMPT');
  let ready = 0; api.onReady(() => ready++); await api.health(); assert.equal(ready, 1);
  api.onReady(() => ready++); await new Promise(resolve => queueMicrotask(resolve)); assert.equal(ready, 2);
  let received; const unsubscribe = api.onEvent(value => { received = value; });
  let connected; const unsubscribeConnected = api.onWatchConnected(value => { connected = value; });
  await api.watch('task-one', 3);
  events.get('task-stream-connected')({ payload: { taskId: 'task-one', conversationId: 'conversation-one' } });
  assert.equal(connected.conversationId, 'conversation-one'); unsubscribeConnected();
  events.get('task-event')({ payload: { taskId: 'task-one', event: { id: 4 } } });
  assert.equal(received.event.id, 4); unsubscribe();
});

test('Rust bridge normalizes rejected IPC messages into handled errors', async () => {
  const { api } = bridgeContext(async () => { throw 'API operation failed (HTTP 404)'; });
  await assert.rejects(api.get('missing'), error => error.message === 'API operation failed (HTTP 404)');
});
