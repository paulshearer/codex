import test from 'node:test';
import './segments-desktop.test.mjs';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { launchDesktop } from './desktop-harness.mjs';

test('message menus fork at a stable point and confirm, retry and replay prefix deletion', async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-history-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const workspace = { id: 'w', name: 'General', workingDirectory: root, skills: [], browserMode: 'dedicated', taskCount: 1 };
  const task = { id: 'original', conversationId: 'original', workspaceId: 'w', threadId: 'native', title: 'Original chat', status: 'running', createdAt: 100,
    request: { prompt: 'First prompt\n' + 'Retained history line.\n'.repeat(90), workingDirectory: root }, result: null };
  let nextId = 0, rejectDelete = true, rejectEdit = true;
  const chats = new Map([['original', { id: 'original', title: 'Original chat', tasks: [task], followups: [], queueHeld: false }]]), events = [], streams = new Set(), actions = [];
  function emit(task, method, params) { events.push({ id: ++nextId, taskId: task.id, kind: 'agent', data: { method, params } }); }
  emit(task, 'item/agentMessage/delta', { itemId: 'a', delta: 'Partial answer' });
  emit(task, 'item/completed', { item: { id: 'a', type: 'agentMessage', text: 'Retained answer' } });
  emit(task, 'item/completed', { item: { id: 'b', type: 'agentMessage', text: 'Later answer' } });
  function snapshot(id) { return { ...chats.get(id), eventCursor: nextId }; }
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    assert.ok(request.headers.signature);
    const url = new URL(request.url, 'http://localhost'), body = raw ? JSON.parse(raw) : null;
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: [workspace] });
    if (url.pathname.startsWith('/v1/skills') || url.pathname.startsWith('/v1/browsers')) return send({ data: [], connections: [] });
    if (url.pathname === '/v1/chats') return send({ data: [...new Map([...chats.values()].map(chat => [chat.chatGroupId || chat.id, chat])).values()].map(chat => ({ ...chat.tasks.at(-1), title: chat.title, chatGroupId: chat.chatGroupId })), nextCursor: null });
    const [, , , id, action] = url.pathname.split('/');
    if (url.pathname.startsWith('/v1/chats/') && request.method === 'POST') {
      actions.push({ id, action, body });
      if (action === 'edit-message') {
        if (rejectEdit) { rejectEdit = false; response.statusCode = 503; return send({}); }
        const point = events.find(event => String(event.id) === body.messageId);
        point.data.params.item.text = body.text;
        chats.get(id).tasks[0].result = body.text;
        return send(snapshot(id));
      }
      if (action === 'delete-message' || action === 'regenerate') {
        const source = chats.get(id).tasks[0];
        const replacement = action === 'delete-message' ? 'single-chat' : 'regenerated-chat';
        const copied = { ...source, id: action === 'delete-message' ? 'single-task' : 'regenerated-task', conversationId: replacement, status: 'completed', result: action === 'delete-message' ? 'Later answer' : 'Regenerated answer' };
        chats.set(replacement, { ...chats.get(id), id: replacement, tasks: [copied], cleanupPending: action !== 'regenerate' });
        if (action !== 'regenerate') chats.delete(id);
        emit(copied, 'item/completed', { item: { id: 'remaining', type: 'agentMessage', text: copied.result } });
        if (action === 'regenerate') {
          assert.equal(body.confirm, undefined);
          const versions = [{ id: 'old-version', conversationId: id }, { id: 'new-version', conversationId: replacement }];
          const original = chats.get(id);
          original.chatGroupId = id;
          original.replyVersions = [{ groupId: 'reply-group', selectedVersionId: 'old-version', taskId: source.id, messageId: String(events.filter(event => event.taskId === source.id).at(-1).id), versions }];
          chats.get(replacement).chatGroupId = id;
          chats.get(replacement).replyVersions = [{ groupId: 'reply-group', selectedVersionId: 'new-version', taskId: copied.id, messageId: String(nextId), versions }];
        }
        return send(snapshot(replacement));
      }
      if (action === 'delete-from' && rejectDelete) { rejectDelete = false; response.statusCode = 409; return send({}); }
      const source = chats.get(id).tasks[0];
      const copied = { ...source, id: action === 'fork' ? 'fork-task' : 'retained-task', conversationId: action === 'fork' ? 'fork' : 'retained-chat', status: 'completed', result: action === 'fork' ? 'Retained answer' : null };
      chats.set(copied.conversationId, { id: copied.conversationId, title: action === 'fork' ? 'Original chat — Fork' : 'Original chat', tasks: [copied], followups: [], queueHeld: false });
      if (action !== 'fork') chats.delete(id);
      if (action === 'fork') emit(copied, 'item/completed', { item: { id: 'a', type: 'agentMessage', text: 'Retained answer' } });
      return send(snapshot(copied.conversationId));
    }
    if (action === 'events') {
      const taskIds = new Set(chats.get(id).tasks.map(task => task.id));
      const data = events.filter(event => taskIds.has(event.taskId) && event.id > Number(url.searchParams.get('after') || 0) && (!url.searchParams.has('through') || event.id <= Number(url.searchParams.get('through'))));
      if (url.searchParams.get('format') === 'json') return send({ data });
      response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n');
      for (const event of data) response.write(`id: ${event.id}\nevent: ${event.kind}\ndata: ${JSON.stringify(event)}\n\n`);
      streams.add(response); response.on('close', () => streams.delete(response)); return;
    }
    if (chats.has(id)) return send(snapshot(id));
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.locator('.task').click();
    const answer = page.locator('.answer').filter({ hasText: 'Retained answer' });
    await answer.getByLabel('Message actions').click();
    const card = await answer.boundingBox(), menuButton = await answer.getByLabel('Message actions').boundingBox();
    assert.ok(menuButton.x > card.x + card.width - 70);
    assert.ok(menuButton.y < card.y + 35);
    assert.equal(await answer.getByRole('menuitem', { name: 'Delete', exact: true }).count(), 1);
    assert.equal(await answer.getByRole('menuitem', { name: 'Regenerate', exact: true }).count(), 1);
    assert.equal(await answer.getByRole('menuitem', { name: 'Fork from here' }).isDisabled(), true);
    assert.equal(await answer.getByRole('menuitem', { name: 'Delete from here' }).isDisabled(), true);
    assert.equal(await answer.getByRole('menuitem', { name: 'Edit', exact: true }).isDisabled(), true);
    task.status = 'completed'; task.completedAt = 120;
    await page.waitForFunction(() => [...document.querySelectorAll('.message-actions button')].some(button => button.textContent === 'Fork from here' && !button.disabled));
    await answer.getByRole('menuitem', { name: 'Fork from here' }).click();
    await page.getByRole('heading', { name: 'Original chat — Fork', exact: true }).waitFor();
    assert.equal(await page.getByText('Later answer', { exact: true }).count(), 0);
    assert.equal(actions[0].body.messageId, '1');
    assert.equal(actions[0].body.taskId, 'original');
    assert.match(actions[0].body.submissionId, /^[0-9a-f-]{36}$/);
    await page.getByLabel('Task prompt').fill('Keep my unsent draft');
    const forkAnswer = page.locator('.answer');
    await forkAnswer.getByLabel('Message actions').click();
    await forkAnswer.getByRole('menuitem', { name: 'Edit', exact: true }).click();
    const editor = page.getByRole('dialog', { name: 'Edit message', exact: true });
    await editor.getByLabel('Message text').fill('Discard this edit');
    await editor.getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal(await forkAnswer.getByText('Retained answer', { exact: true }).count(), 1);
    await forkAnswer.getByLabel('Message actions').click();
    await forkAnswer.getByRole('menuitem', { name: 'Edit', exact: true }).click();
    await editor.getByLabel('Message text').fill('Revised answer 📝');
    await editor.getByRole('button', { name: 'Save message' }).click();
    await editor.getByRole('alert').waitFor();
    assert.equal(await editor.getByLabel('Message text').inputValue(), 'Revised answer 📝');
    await editor.getByRole('button', { name: 'Save message' }).click();
    await editor.waitFor({ state: 'hidden' });
    await forkAnswer.getByText('Revised answer 📝', { exact: true }).waitFor();
    assert.equal(actions[1].body.submissionId, actions[2].body.submissionId);
    const statusRow = await page.locator('.chat-status-row').boundingBox();
    assert.ok(statusRow.height <= 36);
    await page.getByRole('button', { name: 'Stats', exact: true }).click();
    await page.getByRole('dialog', { name: 'Stats', exact: true }).getByRole('button', { name: 'Close', exact: true }).click();
    await forkAnswer.getByLabel('Message actions').click();
    await forkAnswer.getByRole('menuitem', { name: 'Delete from here' }).click();
    const dialog = page.getByRole('dialog', { name: 'Delete from here', exact: true });
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal(actions.length, 3);
    await forkAnswer.getByLabel('Message actions').click();
    await forkAnswer.getByRole('menuitem', { name: 'Delete from here' }).click();
    await dialog.getByRole('button', { name: 'Delete from here', exact: true }).click();
    await dialog.getByRole('alert').waitFor();
    assert.match(await dialog.getByRole('alert').innerText(), /409/);
    await page.screenshot({ path: 'C:/codex-build/permissive-history-actions.png' });
    const scrollBefore = await page.locator('.conversation').evaluate(element => element.scrollTop);
    assert.ok(scrollBefore > 200);
    await dialog.getByRole('button', { name: 'Delete from here', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.querySelectorAll('.answer').length === 0);
    await page.waitForTimeout(500);
    const scrollAfter = await page.locator('.conversation').evaluate(element => ({ top: element.scrollTop, max: element.scrollHeight - element.clientHeight }));
    assert.ok(scrollAfter.top > 200, `Deletion reset scroll to ${scrollAfter.top}`);
    assert.ok(Math.abs(scrollAfter.top - Math.min(scrollBefore, scrollAfter.max)) < 3);
    assert.equal(await page.getByLabel('Task prompt').inputValue(), 'Keep my unsent draft');
    assert.equal(actions[3].body.submissionId, actions[4].body.submissionId);
    assert.equal(actions[4].body.confirm, 'fork');
    assert.equal(await page.locator('.conversation .prompt').count(), 1);
    await page.locator('.task').first().click();
    await page.locator('.answer').filter({ hasText: 'Retained answer' }).getByLabel('Message actions').click();
    await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
    const singleDelete = page.getByRole('dialog', { name: 'Delete message', exact: true });
    await singleDelete.getByRole('button', { name: 'Delete message', exact: true }).click();
    await singleDelete.waitFor({ state: 'hidden' });
    await page.locator('.answer').filter({ hasText: 'Later answer' }).waitFor();
    assert.equal(await page.locator('.answer').filter({ hasText: 'Retained answer' }).count(), 0);
    await page.locator('.answer').filter({ hasText: 'Later answer' }).getByLabel('Message actions').click();
    await page.getByRole('menuitem', { name: 'Regenerate', exact: true }).click();
    assert.equal(await page.getByRole('dialog', { name: 'Regenerate turn', exact: true }).count(), 0);
    await page.locator('.answer').filter({ hasText: 'Regenerated answer' }).waitFor();
    await page.getByText('Reply 2 of 2', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Previous response', exact: true }).click();
    await page.locator('.answer').filter({ hasText: 'Later answer' }).waitFor();
    assert.equal(await page.locator('.answer').filter({ hasText: 'Regenerated answer' }).count(), 0);
    await page.getByText('Reply 1 of 2', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Next response', exact: true }).click();
    await page.locator('.answer').filter({ hasText: 'Regenerated answer' }).waitFor();
    assert.equal(actions.at(-2).action, 'delete-message');
    assert.equal(actions.at(-1).action, 'regenerate');
    await page.locator('.answer').getByLabel('Message actions').click();
    const popup = await page.getByRole('menu').boundingBox();
    assert.ok(popup.y >= 0 && popup.y + popup.height <= await page.evaluate(() => innerHeight), 'All turn actions fit on screen');
    await page.getByRole('menuitem', { name: 'Regenerate', exact: true }).waitFor({ state: 'visible' });
    await page.screenshot({ path: 'C:/codex-build/permissive-turn-menu.png' });
    await page.getByLabel('Task prompt').click();
    assert.equal(await page.getByRole('menu').count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    if (app) await app.close();
    for (const stream of streams) stream.end();
    await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(home), tmpdir());
    rmSync(home, { recursive: true, force: true });
  }
});
