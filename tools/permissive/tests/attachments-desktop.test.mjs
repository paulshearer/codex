import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { launchDesktop } from './desktop-harness.mjs';

test('desktop pastes images, attaches native-picked files, removes drafts and retains attachments for retries', async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-attachments-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const filename = path.join(home, 'notes.txt'); writeFileSync(filename, 'ATTACHMENT-NATIVE-PICKER');
  const workspace = { id: 'w0', name: 'General', workingDirectory: root, skills: [], browserMode: 'dedicated', browser: 'chromium', taskCount: 0 };
  const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQ1cAAAAASUVORK5CYII=';
  const attachments = new Map(), tasks = [], submitted = [], streams = new Set();
  let failOnce = true, clipboardUploads = 0, pickerUploads = 0;
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    assert.ok(request.headers.signature);
    const body = raw ? JSON.parse(raw) : null, url = new URL(request.url, 'http://localhost');
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: [workspace] });
    if (url.pathname.startsWith('/v1/browsers') || url.pathname.startsWith('/v1/skills')) return send({ data: [], connections: [] });
    if (url.pathname === '/v1/attachments') {
      assert.equal(body.workspaceId, 'w0');
      const image = body.name.endsWith('.png'), data = Buffer.from(body.data, 'base64');
      if (image) { clipboardUploads++; assert.equal(body.data, png); }
      else { pickerUploads++; assert.equal(data.toString(), 'ATTACHMENT-NATIVE-PICKER'); }
      const attachment = { id: randomUUID(), name: body.name, size: data.length, mime: image ? 'image/png' : 'application/octet-stream', ...(image ? { preview: `data:image/png;base64,${png}` } : {}) };
      attachments.set(attachment.id, attachment); response.statusCode = 201; return send(attachment);
    }
    if (url.pathname.startsWith('/v1/attachments/')) {
      const id = url.pathname.split('/')[3];
      if (request.method === 'DELETE') { assert.ok(attachments.delete(id)); return send({ removed: true }); }
      return send(attachments.get(id));
    }
    if (url.pathname === '/v1/tasks' && request.method === 'POST') {
      submitted.push(body);
      if (failOnce) { failOnce = false; response.statusCode = 503; return send({}); }
      const task = { id: 't0', conversationId: 't0', workspaceId: 'w0', threadId: 'native-t0', status: 'completed', createdAt: 100, updatedAt: 101, request: { ...body, workingDirectory: root }, result: 'Attachments received', artifacts: [{ id: 'report.zip', name: 'report.zip', size: 100 }] };
      tasks.push(task); workspace.taskCount = 1; return send(task);
    }
    if (url.pathname === '/v1/tasks/t0/continue') {
      submitted.push(body);
      const task = { ...tasks[0], id: 't1', request: { ...body, workingDirectory: root }, result: 'Follow-up received', artifacts: [] };
      tasks.push(task); return send(task);
    }
    if (url.pathname === '/v1/chats') return send({ data: tasks.length ? [{ ...tasks.at(-1), title: tasks[0].request.prompt }] : [], nextCursor: null });
    if (url.pathname.endsWith('/events')) {
      if (url.searchParams.get('format') === 'json') return send({ data: [] });
      response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n'); streams.add(response); response.on('close', () => streams.delete(response)); return;
    }
    if (url.pathname === '/v1/chats/t0') return send({ id: 't0', tasks, followups: [], eventCursor: 0 });
    if (url.pathname.startsWith('/v1/tasks/')) return send(tasks.find(task => url.pathname.endsWith('/' + task.id)));
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}`, dialogs: { attachments: [filename] } });
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.getByText('● Service connected').waitFor();
    await page.locator('.workspace.active').waitFor();
    await page.getByRole('button', { name: 'Run task', exact: true }).waitFor();
    await page.getByLabel('Task prompt').fill('ready');
    await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(button => button.textContent === 'Run task').disabled);
    await page.getByLabel('Task prompt').fill('');
    await page.getByLabel('Task prompt').evaluate((input, png) => {
      const clipboardData = new DataTransfer();
      clipboardData.items.add(new File([Uint8Array.from(atob(png), char => char.charCodeAt(0))], 'clipboard.png', { type: 'image/png' }));
      assertPaste(input, clipboardData);
      function assertPaste(input, clipboardData) { if (input.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData }))) throw new Error('Image paste was not captured'); }
    }, png);
    const draft = page.getByRole('list', { name: 'Draft attachments', exact: true });
    await draft.getByRole('img', { name: 'Preview of clipboard.png' }).waitFor();
    assert.equal(await draft.getByRole('img').evaluate(image => image.complete && image.naturalWidth > 0), true);
    assert.equal(await page.getByRole('button', { name: 'Run task', exact: true }).isEnabled(), true);
    await page.getByLabel('Chat options', { exact: true }).click();
    await page.getByRole('menuitem', { name: 'Attach files…', exact: true }).click();
    await draft.getByText('notes.txt', { exact: true }).waitFor();
    await draft.getByRole('button', { name: 'Remove attachment notes.txt' }).click();
    await page.waitForFunction(() => document.querySelectorAll('[aria-label="Draft attachments"] li').length === 1);
    await page.getByLabel('Chat options', { exact: true }).click();
    await page.getByRole('menuitem', { name: 'Attach files…', exact: true }).click();
    await draft.getByText('notes.txt', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Run task', exact: true }).click();
    await page.getByRole('alert').waitFor();
    assert.equal(await draft.locator('li').count(), 2);
    await page.getByRole('button', { name: 'Run task', exact: true }).click();
    await page.getByText('Attachments received', { exact: true }).waitFor();
    assert.deepEqual(submitted[0], submitted[1]);
    assert.equal(submitted[1].prompt, 'Please examine the attached files.');
    assert.equal(submitted[1].attachments.length, 2);
    assert.equal(await page.getByRole('list', { name: 'Message attachments', exact: true }).count(), 0);
    assert.equal(await page.getByText('report.zip', { exact: true }).count(), 0);
    await page.getByRole('button', { name: 'Attachments (3)', exact: true }).click();
    const panel = page.getByRole('dialog', { name: 'Attachments', exact: true });
    await panel.waitFor();
    const sent = page.getByRole('list', { name: 'Message attachments', exact: true });
    await sent.getByRole('img', { name: 'Preview of clipboard.png' }).waitFor();
    await sent.getByText('notes.txt', { exact: true }).waitFor();
    await panel.getByText('report.zip', { exact: true }).waitFor();
    await page.screenshot({ path: 'C:/codex-build/permissive-attachments-panel.png' });
    assert.equal(await draft.count(), 0);
    assert.equal(clipboardUploads, 1); assert.equal(pickerUploads, 2);
    await page.keyboard.press('Escape');
    assert.equal(await panel.count(), 0);
    assert.equal(await sent.count(), 0);
    await page.getByLabel('Task prompt').evaluate(input => {
      const clipboardData = new DataTransfer(); clipboardData.setData('text/plain', 'ordinary clipboard text');
      if (!input.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData }))) throw new Error('Ordinary text paste was blocked');
    });
    await page.getByLabel('Task prompt').fill('Continue without attachments');
    await page.getByRole('button', { name: 'Continue conversation', exact: true }).click();
    await page.getByText('Follow-up received', { exact: true }).waitFor();
    assert.equal(submitted.at(-1).attachments, undefined);
    await page.getByRole('button', { name: 'Attachments (3)', exact: true }).click();
    assert.equal(await page.getByRole('list', { name: 'Message attachments', exact: true }).count(), 1);
    await page.getByRole('button', { name: 'Close attachments', exact: true }).click();
    await page.screenshot({ path: 'C:/codex-build/permissive-attachments-ui.png' });
    assert.deepEqual(errors, []);
  } finally {
    if (app) await app.close();
    for (const stream of streams) stream.destroy();
    await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(home), tmpdir()); rmSync(home, { recursive: true, force: true });
  }
});
