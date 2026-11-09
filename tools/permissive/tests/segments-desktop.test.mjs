import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { launchDesktop } from './desktop-harness.mjs';

test('latest segment opens alone; previous loads once and remains through refresh', async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-segments-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const workspace = { id: 'w', name: 'General', workingDirectory: root, skills: [], browserMode: 'dedicated', taskCount: 1 };
  const task = number => ({ id: `task-${number}`, conversationId: 'chat', workspaceId: 'w', segmentId: `chat-${number}`, status: 'completed', createdAt: 100,
    request: { prompt: `Prompt ${number}`, workingDirectory: root }, result: `Answer ${number}\n` + 'History line.\n'.repeat(50) });
  const snapshot = number => ({ id: 'chat', title: 'Segmented chat', tasks: [task(number)], followups: [], queueHeld: false,
    segment: { id: `chat-${number}`, number, latestId: 'chat-3', previousId: number > 1 ? `chat-${number - 1}` : null, after: number * 100 }, eventCursor: number * 100 });
  const reads = [], actions = [], streams = new Set();
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://localhost');
    let body = ''; for await (const part of request) body += part;
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: [workspace] });
    if (url.pathname === '/v1/chats') return send({ data: [{ ...task(3), title: 'Segmented chat' }], nextCursor: null });
    if (url.pathname.startsWith('/v1/skills') || url.pathname.startsWith('/v1/browsers')) return send({ data: [], connections: [] });
    if (url.pathname.endsWith('/events')) {
      if (url.searchParams.get('format') === 'json') return send({ data: [], nextCursor: Number(url.searchParams.get('through')) });
      response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n'); streams.add(response); response.on('close', () => streams.delete(response)); return;
    }
    if (url.pathname === '/v1/chats/chat' && request.method === 'GET') {
      const number = Number(url.searchParams.get('segmentId')?.split('-').at(-1) || 3); reads.push(number); return send(snapshot(number));
    }
    if (url.pathname.endsWith('/fork')) { actions.push(JSON.parse(body)); return send(snapshot(3)); }
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
    const page = await app.firstWindow();
    await page.locator('.task').click();
    await page.getByText('Prompt 3', { exact: true }).waitFor();
    assert.equal(await page.getByText('Prompt 2', { exact: true }).count(), 0);
    assert.ok(reads.every(number => number === 3));
    await page.getByRole('button', { name: 'Load previous segment', exact: true }).click();
    await page.getByText('Prompt 2', { exact: true }).waitFor();
    assert.equal(reads.filter(number => number === 2).length, 1);
    assert.equal(reads.includes(1), false);
    await page.waitForTimeout(2400);
    assert.equal(await page.getByText('Prompt 2', { exact: true }).count(), 1);
    const answer = page.locator('.answer').filter({ hasText: 'Answer 2' });
    await answer.getByLabel('Message actions').click();
    await answer.getByRole('menuitem', { name: 'Fork from here' }).click();
    for (let retry = 0; retry < 40 && !actions.length; retry++) await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(actions[0].segmentId, 'chat-2');
    assert.equal(actions[0].taskId, 'task-2');
    await page.getByRole('button', { name: 'New task', exact: false }).click();
    await page.waitForFunction(() => !document.querySelector('.conversation-turn'));
    assert.equal(await page.getByRole('button', { name: 'Load previous segment', exact: true }).count(), 0);
  } finally {
    if (app) await app.close();
    for (const stream of streams) stream.end();
    await new Promise(resolve => server.close(resolve));
    rmSync(home, { recursive: true, force: true });
  }
});
