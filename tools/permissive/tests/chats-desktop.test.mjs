import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { launchDesktop } from './desktop-harness.mjs';

test('desktop interleaves activity, steers with retry, holds follow-ups and switches chat views', async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-chats-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const workspaces = ['General', 'Other'].map((name, index) => ({ id: 'w' + index, name, workingDirectory: root,
    skills: [], browserMode: 'dedicated', browser: 'chromium', executionDetail: 'standard', diagnosticLogLevel: 'off', taskCount: 1 }));
  const first = { id: 't0', conversationId: 't0', workspaceId: 'w0', threadId: 'native-t0', activeTurnId: 'turn0',
    status: 'running', createdAt: 100, updatedAt: 120, request: { prompt: 'Active conversation', workingDirectory: root } };
  const other = { ...first, id: 't1', conversationId: 't1', workspaceId: 'w1', status: 'completed', result: 'Other answer', request: { ...first.request, prompt: 'Other conversation' } };
  const tasks = [first, other], events = [], streams = new Set(), followups = [], submittedIds = [];
  let held = false, uncertainOnce = true, renameFailure = true;
  const titles = new Map(), renames = [];
  const deliveries = new Set();
  function emit(task, kind, data) {
    const event = { id: events.length + 1, taskId: task.id, kind, data, createdAt: 120 };
    events.push(event);
    for (const stream of streams) stream.write(`id: ${event.id}\nevent: ${kind}\ndata: ${JSON.stringify(event)}\n\n`);
  }
  emit(first, 'agent', { method: 'item/completed', params: { item: { id: 'a', type: 'agentMessage', phase: 'commentary', text: 'Checking the project.' } } });
  emit(first, 'agent', { method: 'item/started', params: { item: { id: 'cmd', type: 'commandExecution', command: 'Inspect project' } } });
  emit(first, 'agent', { method: 'item/completed', params: { item: { id: 'cmd', type: 'commandExecution', command: 'Inspect project', aggregatedOutput: 'TOOL-OUTPUT', status: 'completed' } } });
  emit(first, 'agent', { method: 'item/completed', params: { item: { id: 'b', type: 'agentMessage', phase: 'commentary', text: 'The project is ready.' } } });
  function chat(id) { return { id, title: titles.get(id) || tasks.find(task => task.conversationId === id)?.request.prompt, tasks: tasks.filter(task => task.conversationId === id), followups: id === 't0' ? followups : [], queueHeld: held, eventCursor: events.at(-1)?.id || 0 }; }
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    assert.ok(request.headers.signature);
    const url = new URL(request.url, 'http://localhost'), body = raw ? JSON.parse(raw) : null;
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: workspaces });
    if (url.pathname.startsWith('/v1/workspaces/') && request.method === 'PATCH') {
      const workspace = workspaces.find(value => url.pathname.endsWith('/' + value.id)); Object.assign(workspace, body); return send(workspace);
    }
    if (url.pathname.startsWith('/v1/browsers') || url.pathname.startsWith('/v1/skills')) return send({ data: [], connections: [] });
    if (url.pathname === '/v1/chats') {
      const scope = url.searchParams.get('workspaceId');
      return send({ data: [...new Map(tasks.filter(task => !scope || scope === task.workspaceId).map(task => [task.conversationId, { ...task, title: titles.get(task.conversationId) || tasks.find(value => value.conversationId === task.conversationId).request.prompt }])).values()], nextCursor: null });
    }
    if (url.pathname.startsWith('/v1/chats/') && request.method === 'PATCH') {
      const id = url.pathname.split('/')[3]; renames.push({ id, ...body });
      if (renameFailure) { renameFailure = false; response.statusCode = 503; return send({}); }
      titles.set(id, body.title); return send({ id, title: body.title });
    }
    if (url.pathname === '/v1/tasks/t0/steer') {
      submittedIds.push(body.submissionId);
      assert.equal(body.expectedTurnId, 'turn0');
      if (!deliveries.has(body.submissionId)) {
        deliveries.add(body.submissionId);
        emit(first, 'agent', { method: 'item/completed', params: { item: { id: 'native-' + body.submissionId, clientId: body.submissionId, type: 'userMessage' } } });
        emit(first, 'steering', { ...body, turnId: 'turn0' });
      }
      if (uncertainOnce) { uncertainOnce = false; response.statusCode = 500; return send({}); }
      return send({ turnId: 'turn0' });
    }
    if (url.pathname === '/v1/tasks/t2/continue') {
      const previous = tasks.find(task => task.id === 't2');
      const next = { ...previous, id: 't3', status: 'completed', activeTurnId: null, result: 'CONTINUED-RESULT', request: { ...previous.request, ...body } };
      tasks.push(next);
      emit(next, 'agent', { method: 'item/completed', params: { item: { id: 'final', type: 'agentMessage', text: next.result } } });
      emit(next, 'status', { status: 'completed' });
      return send(next);
    }
    if (url.pathname === '/v1/chats/t0/followups' && request.method === 'POST') {
      followups.push({ id: body.submissionId, body }); emit(first, 'queue', { status: 'waiting' }); return send(chat('t0'));
    }
    if (url.pathname === '/v1/chats/t0/followups/resume') {
      held = false;
      const next = followups.shift();
      const successor = { ...first, id: 't2', status: 'running', activeTurnId: 'turn2', request: { ...first.request, ...next.body } };
      tasks.push(successor); emit(successor, 'status', { status: 'running' }); return send(chat('t0'));
    }
    if (url.pathname.startsWith('/v1/chats/') && url.pathname.endsWith('/events')) {
      const id = url.pathname.split('/')[3], values = events.filter(event => tasks.find(task => task.id === event.taskId)?.conversationId === id
        && event.id > Number(url.searchParams.get('after') || 0) && (!url.searchParams.has('through') || event.id <= Number(url.searchParams.get('through'))));
      if (url.searchParams.get('format') === 'json') return send({ data: values });
      response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n');
      for (const event of values) response.write(`id: ${event.id}\nevent: ${event.kind}\ndata: ${JSON.stringify(event)}\n\n`);
      streams.add(response); response.on('close', () => streams.delete(response)); return;
    }
    if (url.pathname.startsWith('/v1/chats/')) return send(chat(url.pathname.split('/')[3]));
    if (url.pathname.startsWith('/v1/tasks/')) return send(tasks.find(task => url.pathname.endsWith('/' + task.id)));
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.locator('.task').click();
    await page.locator('.answer').first().waitFor();
    await page.getByLabel('Task prompt').fill('Keep my unsent draft');
    await page.getByRole('button', { name: 'Rename chat', exact: true }).click();
    const renameDialog = page.getByRole('dialog', { name: 'Rename chat', exact: true });
    assert.equal(await page.getByLabel('Chat name', { exact: true }).inputValue(), 'Active conversation');
    await page.getByLabel('Chat name', { exact: true }).fill('   ');
    assert.equal(await renameDialog.getByRole('button', { name: 'Save', exact: true }).isDisabled(), true);
    await page.getByLabel('Chat name', { exact: true }).fill('x'.repeat(121));
    assert.equal(await renameDialog.getByRole('button', { name: 'Save', exact: true }).isDisabled(), true);
    await page.getByLabel('Chat name', { exact: true }).fill('  Active conversation renamed 📝  ');
    await renameDialog.getByRole('button', { name: 'Save', exact: true }).click();
    await renameDialog.getByRole('alert').waitFor();
    assert.match(await renameDialog.getByRole('alert').innerText(), /HTTP 503/);
    assert.equal(await page.getByLabel('Chat name', { exact: true }).inputValue(), '  Active conversation renamed 📝  ');
    await renameDialog.getByRole('button', { name: 'Save', exact: true }).click();
    await renameDialog.waitFor({ state: 'hidden' });
    await page.getByRole('heading', { name: 'Active conversation renamed 📝', exact: true }).waitFor();
    assert.equal(await page.getByLabel('Task prompt').inputValue(), 'Keep my unsent draft');
    assert.deepEqual(renames, [{ id: 't0', title: 'Active conversation renamed 📝' }, { id: 't0', title: 'Active conversation renamed 📝' }]);
    assert.equal(first.request.prompt, 'Active conversation');
    await page.getByRole('button', { name: 'Rename chat', exact: true }).click();
    await page.getByLabel('Chat name', { exact: true }).fill('Discard this title');
    await renameDialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal(renames.length, 2);
    assert.deepEqual(await page.locator('.conversation-turn').first().locator(':scope > .answer').evaluateAll(nodes => nodes.map(node => node.className)), ['answer', 'answer']);
    await page.getByRole('button', { name: 'Activity', exact: true }).click();
    assert.equal(await page.getByText('TOOL-OUTPUT', { exact: true }).isVisible(), false);
    await page.locator('.execution-details > summary').click();
    assert.equal(await page.getByText('TOOL-OUTPUT', { exact: true }).isVisible(), true);
    await page.getByRole('dialog', { name: 'Activity', exact: true }).getByRole('button', { name: 'Close', exact: true }).click();
    assert.equal(await page.locator('.live-progress').evaluate(element => getComputedStyle(element).color), 'rgb(196, 199, 204)');
    await page.getByLabel('Task prompt').fill('Please use the smaller change');
    await page.getByRole('button', { name: 'Steer now', exact: true }).click();
    await page.getByRole('alert').waitFor();
    assert.equal(await page.getByLabel('Task prompt').inputValue(), 'Please use the smaller change');
    await page.getByRole('button', { name: 'Steer now', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[aria-label="Task prompt"]').value === '');
    assert.equal(submittedIds[0], submittedIds[1]); assert.equal(deliveries.size, 1);
    assert.equal(await page.locator('.conversation .prompt').filter({ hasText: 'Please use the smaller change' }).count(), 1);
    await page.getByLabel('Task prompt').fill('Follow-up after this work');
    await page.getByRole('button', { name: 'Queue follow-up', exact: true }).click();
    await page.getByText('Queued follow-ups', { exact: true }).waitFor();
    Object.assign(first, { status: 'cancelled', activeTurnId: null, completedAt: 130 }); held = true;
    emit(first, 'status', { status: 'cancelled' });
    await page.getByText('Follow-ups held', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Resume queue', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.conversation-turn').length === 2);
    assert.equal(await page.locator('.task').count(), 1);
    const successor = tasks.find(task => task.id === 't2');
    Object.assign(successor, { status: 'completed', activeTurnId: null, result: 'SECOND-RESULT', completedAt: 140 });
    emit(successor, 'agent', { method: 'item/completed', params: { item: { id: 'final', type: 'agentMessage', text: successor.result } } });
    emit(successor, 'status', { status: 'completed' });
    await page.getByRole('button', { name: 'Continue conversation', exact: true }).waitFor();
    await page.getByLabel('Task prompt').fill('Continue this existing chat');
    await page.getByRole('button', { name: 'Continue conversation', exact: true }).click();
    await page.getByText('CONTINUED-RESULT', { exact: true }).waitFor();
    assert.equal(await page.locator('.conversation-turn').count(), 3);
    assert.equal(tasks.at(-1).threadId, first.threadId);
    assert.equal(await page.getByText('SECOND-RESULT', { exact: true }).count(), 1);
    for (const stream of streams) stream.destroy();
    emit(tasks.at(-1), 'agent', { method: 'item/completed', params: { item: { id: 'reconnected', type: 'agentMessage', text: 'RECONNECTED-RESULT' } } });
    await page.getByText('RECONNECTED-RESULT', { exact: true }).waitFor();
    await page.getByText('Chat connection interrupted. Reconnecting…', { exact: true }).waitFor({ state: 'hidden' });
    assert.equal(await page.getByText('CONTINUED-RESULT', { exact: true }).count(), 1);
    await page.screenshot({ path: 'C:/codex-build/permissive-continuous-chats.png' });
    await page.getByLabel('Chat view').selectOption('chronological');
    await page.waitForFunction(() => document.querySelectorAll('.task').length === 2);
    await page.getByRole('button', { name: 'Rename chat Other conversation', exact: true }).click();
    await page.getByLabel('Chat name', { exact: true }).fill('Other renamed without opening');
    await renameDialog.getByRole('button', { name: 'Save', exact: true }).click();
    await renameDialog.waitFor({ state: 'hidden' });
    assert.equal(renames.at(-1).id, 't1');
    assert.equal(await page.getByRole('heading', { level: 1 }).innerText(), 'Active conversation renamed 📝');
    titles.set('t0', 'Active conversation changed on Android');
    await page.getByRole('heading', { name: 'Active conversation changed on Android', exact: true }).waitFor();
    assert.equal(await page.locator('.conversation-turn').count(), 3);
    await page.locator('.task').filter({ hasText: 'Other' }).click();
    await page.getByText('Other answer', { exact: true }).waitFor();
    assert.equal(await page.locator('.workspace-caption').innerText(), 'Other');
    await page.locator('.task').filter({ hasText: 'Active conversation' }).click();
    await page.getByText('CONTINUED-RESULT', { exact: true }).waitFor();
    assert.equal(await page.locator('.conversation-turn').count(), 3);
    await page.locator('.task').filter({ hasText: 'Other' }).click();
    await page.getByText('Other answer', { exact: true }).waitFor();
    await page.getByLabel('Chat view').selectOption('workspace');
    await page.waitForFunction(() => document.querySelectorAll('.task').length === 1);
    await page.getByRole('button', { name: 'Workspace settings for Other', exact: true }).click();
    await page.getByLabel('Execution detail').selectOption('full');
    await page.getByLabel('Diagnostic logging').selectOption('debug');
    await page.getByRole('button', { name: 'Save workspace', exact: true }).click();
    assert.equal(workspaces[1].executionDetail, 'full'); assert.equal(workspaces[1].diagnosticLogLevel, 'debug');
    assert.deepEqual(errors, []);
  } finally {
    if (app) await app.close();
    for (const stream of streams) stream.end();
    await new Promise(resolve => server.close(resolve));
    rmSync(home, { recursive: true, force: true });
  }
});

test('desktop confirms deletion with a button, retains failed deletions for retry and clears deleted drafts', async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-delete-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const workspaces = ['General', 'Disposable'].map((name, index) => ({ id: `w${index}`, name, workingDirectory: root,
    skills: [], browserMode: 'dedicated', browser: 'chromium', taskCount: 1 }));
  const tasks = workspaces.map((workspace, index) => ({ id: `task${index}`, conversationId: `chat${index}`, threadId: `native${index}`,
    workspaceId: workspace.id, title: `Saved chat ${index}`, status: 'completed', createdAt: 100, completedAt: 120,
    request: { prompt: `Saved chat ${index}`, workingDirectory: root }, result: `Saved answer ${index}` }));
  const deletionRequests = [], streams = new Set();
  let rejectOnce = true, finishDeletion, deletionEntered;
  const deletionPending = new Promise(resolve => { deletionEntered = resolve; });
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    assert.ok(request.headers.signature);
    const url = new URL(request.url, 'http://localhost');
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: workspaces });
    if (url.pathname.startsWith('/v1/browsers') || url.pathname.startsWith('/v1/skills')) return send({ data: [], connections: [] });
    if (url.pathname === '/v1/chats') return send({ data: tasks.filter(task => !url.searchParams.get('workspaceId') || task.workspaceId === url.searchParams.get('workspaceId')), nextCursor: null });
    if (url.pathname.endsWith('/secure-delete')) {
      const workspace = url.pathname.startsWith('/v1/workspaces/'), id = url.pathname.split('/')[3];
      deletionRequests.push({ path: url.pathname, body: JSON.parse(raw) });
      assert.deepEqual(JSON.parse(raw), { confirm: id, passes: 7 });
      if (rejectOnce) { rejectOnce = false; response.statusCode = 409; return send({ error: 'Finish active tasks first' }); }
      if (!workspace) await new Promise(resolve => { finishDeletion = resolve; deletionEntered(); });
      const removed = tasks.filter(task => workspace ? task.workspaceId === id : task.conversationId === id);
      tasks.splice(0, tasks.length, ...tasks.filter(task => !removed.includes(task)));
      if (workspace) workspaces.splice(workspaces.findIndex(value => value.id === id), 1);
      return send({ conversationIds: removed.map(task => task.conversationId), taskIds: removed.map(task => task.id), threadIds: removed.map(task => task.threadId) });
    }
    if (url.pathname.startsWith('/v1/chats/')) {
      const id = url.pathname.split('/')[3], selected = tasks.filter(task => task.conversationId === id);
      if (url.pathname.endsWith('/events')) {
        if (url.searchParams.get('format') === 'json') return send({ data: [] });
        response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n');
        streams.add(response); response.on('close', () => streams.delete(response)); return;
      }
      return send({ id, tasks: selected, followups: [], queueHeld: false, eventCursor: 0 });
    }
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.locator('.task').click();
    await page.getByRole('button', { name: 'Continue conversation', exact: true }).waitFor();
    await page.evaluate(() => {
      for (const id of ['chat0', 'task0', 'native0', 'chat1', 'task1', 'native1', 'unrelated']) localStorage.setItem(`permissive-chat-options:${id}`, 'saved');
    });
    const dialog = page.getByRole('dialog', { name: 'Secure deletion', exact: true });
    await page.getByRole('button', { name: 'Securely delete chat Saved chat 0', exact: true }).click();
    assert.equal(await dialog.getByRole('textbox').count(), 0);
    assert.equal(await dialog.getByRole('button', { name: 'Delete permanently · 7 passes', exact: true }).isEnabled(), true);
    await dialog.getByRole('button', { name: 'Keep chat', exact: true }).click();
    assert.equal(deletionRequests.length, 0);
    await page.getByRole('button', { name: 'Securely delete chat Saved chat 0', exact: true }).click();
    await dialog.getByRole('button', { name: 'Delete permanently · 7 passes', exact: true }).click();
    await dialog.getByRole('alert').waitFor();
    assert.match(await dialog.getByRole('alert').innerText(), /HTTP 409/);
    assert.match(await dialog.getByRole('alert').innerText(), /Finish or cancel running and paused tasks, then retry/);
    assert.equal(await page.locator('.task').count(), 1);
    await dialog.getByRole('button', { name: 'Delete permanently · 7 passes', exact: true }).click();
    await dialog.getByRole('button', { name: 'Overwriting…', exact: true }).waitFor();
    assert.equal(await dialog.getByRole('button', { name: 'Overwriting…', exact: true }).isDisabled(), true);
    assert.equal(await dialog.getByRole('button', { name: 'Keep chat', exact: true }).isDisabled(), true);
    await deletionPending;
    finishDeletion(); finishDeletion = null;
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('heading', { name: 'What should we work on?', exact: true }).waitFor();
    assert.equal(await page.locator('.task').count(), 0);
    assert.deepEqual(await page.evaluate(() => ['chat0', 'task0', 'native0'].map(id => localStorage.getItem(`permissive-chat-options:${id}`))), [null, null, null]);
    await page.getByRole('button', { name: 'Securely delete workspace Disposable', exact: true }).click();
    assert.equal(await dialog.getByRole('textbox').count(), 0);
    await dialog.getByRole('button', { name: 'Delete permanently · 7 passes', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'Securely delete workspace Disposable', exact: true }).waitFor({ state: 'hidden' });
    assert.deepEqual(await page.evaluate(() => ['chat1', 'task1', 'native1'].map(id => localStorage.getItem(`permissive-chat-options:${id}`))), [null, null, null]);
    assert.equal(await page.evaluate(() => localStorage.getItem('permissive-chat-options:unrelated')), 'saved');
    assert.equal(deletionRequests.length, 3);
    assert.deepEqual(errors, []);
  } finally {
    finishDeletion?.();
    if (app) await app.close();
    for (const stream of streams) stream.end();
    await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(home), tmpdir());
    rmSync(home, { recursive: true, force: true });
  }
});

test('desktop displays response recovery and retries the existing task without resubmitting its prompt', async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-recovery-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const workspace = { id: 'w', name: 'Recovery', workingDirectory: root, skills: [], browserMode: 'dedicated', browser: 'chromium', taskCount: 1 };
  const createdAt = Math.floor(Date.now() / 1000) - 20;
  const task = { id: 't', conversationId: 'chat', threadId: 'native', workspaceId: 'w', title: 'Original request',
    status: 'failed', canRetry: false, error: 'Responses completed event has no completed response', createdAt, completedAt: createdAt + 10,
    request: { prompt: 'Original request', workingDirectory: root } };
  const events = [], streams = new Set(), mutations = [];
  let rejectOnce = true, finishRetry, retryEntered;
  const retryPending = new Promise(resolve => { retryEntered = resolve; });
  function emit(kind, data) {
    const event = { id: events.length + 1, taskId: task.id, kind, data, createdAt: Math.floor(Date.now() / 1000) };
    events.push(event);
    for (const stream of streams) stream.write(`id: ${event.id}\nevent: ${kind}\ndata: ${JSON.stringify(event)}\n\n`);
  }
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    assert.ok(request.headers.signature);
    const url = new URL(request.url, 'http://localhost');
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (request.method === 'POST') mutations.push({ path: url.pathname, body: raw });
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: [workspace] });
    if (url.pathname.startsWith('/v1/browsers') || url.pathname.startsWith('/v1/skills')) return send({ data: [], connections: [] });
    if (url.pathname === '/v1/chats') return send({ data: [task], nextCursor: null });
    if (url.pathname === '/v1/chats/chat') return send({ id: 'chat', tasks: [task], followups: [], queueHeld: false, eventCursor: events.length });
    if (url.pathname === '/v1/tasks/t') return send(task);
    if (url.pathname === '/v1/chats/chat/events') {
      const values = events.filter(event => event.id > Number(url.searchParams.get('after') || 0) && (!url.searchParams.has('through') || event.id <= Number(url.searchParams.get('through'))));
      if (url.searchParams.get('format') === 'json') return send({ data: values });
      response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n');
      for (const event of values) response.write(`id: ${event.id}\nevent: ${event.kind}\ndata: ${JSON.stringify(event)}\n\n`);
      streams.add(response); response.on('close', () => streams.delete(response)); return;
    }
    if (url.pathname === '/v1/tasks/t/retry' && request.method === 'POST') {
      assert.equal(raw, '');
      if (rejectOnce) { rejectOnce = false; response.statusCode = 409; return send({}); }
      await new Promise(resolve => { finishRetry = resolve; retryEntered(); });
      Object.assign(task, { status: 'running', canRetry: false, error: null, completedAt: null });
      emit('recovery', { phase: 'requested', message: 'Retrying the existing conversation.' });
      emit('recovery', { phase: 'retrying', attempt: 1, maxAttempts: 2, message: 'Reconnecting to the response.' });
      return send(task);
    }
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.locator('.task').click();
    await page.getByText(task.error, { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Retry response', exact: true }).count(), 0);
    task.canRetry = true;
    emit('recovery', { phase: 'exhausted', attempt: 2, maxAttempts: 2, message: 'Automatic recovery did not finish. You can retry this response.' });
    await page.getByText('Automatic response retries exhausted', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Retry response', exact: true }).click();
    await page.getByRole('alert').waitFor();
    assert.match(await page.getByRole('alert').innerText(), /HTTP 409/);
    await page.getByRole('button', { name: 'Retry response', exact: true }).click();
    const retrying = page.getByRole('button', { name: 'Retrying response…', exact: true });
    await retrying.waitFor();
    assert.equal(await retrying.isDisabled(), true);
    await retryPending;
    finishRetry(); finishRetry = null;
    await page.getByText('Retrying response · 1 of 2', { exact: true }).waitFor();
    await page.getByText('Reconnecting to the response.', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Retry response', exact: true }).count(), 0);
    await page.screenshot({ path: 'C:/codex-build/permissive-response-recovery.png' });
    Object.assign(task, { status: 'completed', completedAt: Math.floor(Date.now() / 1000), result: 'Recovered answer' });
    emit('agent', { method: 'item/completed', params: { item: { id: 'answer', type: 'agentMessage', text: task.result } } });
    emit('recovery', { phase: 'recovered', attempt: 1, maxAttempts: 2, message: 'The response recovered in the same conversation.' });
    emit('status', { status: 'completed' });
    await page.getByText('Recovered answer', { exact: true }).waitFor();
    await page.getByText('Retrying response · 1 of 2', { exact: true }).waitFor({ state: 'hidden' });
    assert.equal(await page.getByText('Response recovered', { exact: true }).count(), 0);
    assert.equal(await page.locator('.conversation-turn').count(), 1);
    assert.equal(await page.locator('.conversation .prompt').count(), 1);
    assert.equal(await page.locator('.task').count(), 1);
    assert.deepEqual(mutations, [{ path: '/v1/tasks/t/retry', body: '' }, { path: '/v1/tasks/t/retry', body: '' }]);
    assert.equal(task.id, 't'); assert.equal(task.threadId, 'native');
    assert.deepEqual(errors, []);
  } finally {
    finishRetry?.();
    if (app) await app.close();
    for (const stream of streams) stream.end();
    await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(home), tmpdir());
    rmSync(home, { recursive: true, force: true });
  }
});
