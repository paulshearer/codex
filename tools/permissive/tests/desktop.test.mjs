import test from 'node:test';
import assert from 'node:assert/strict';
import { launchDesktop } from './desktop-harness.mjs';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { readFileSync, mkdtempSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createHash, createPublicKey, verify } from 'node:crypto';

test('independent desktop displays tasks and submits through its isolated IPC', async () => {
  const python = process.env.PERMISSIVE_PYTHON || 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const root = process.cwd();
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-signed-desktop-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], {
    env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore',
  });
  const entry = JSON.parse(readFileSync(path.join(home, 'auth/authorized-keys.json'), 'utf8')).keys.find(value => value.scopes.includes('api'));
  const raw = Buffer.from(entry.publicKey.split(' ')[1], 'base64').subarray(-32);
  const publicKey = createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), raw]), format: 'der', type: 'spki' });
  const nonces = new Set();
  const tasks = [];
  let setupRequests = 0, browserCatalogGate = null;
  const connectedProfiles = new Set();
  const workspaces = [{ id: 'workspace-general', name: 'General', workingDirectory: root, browserMode: 'dedicated', systemPrompt: '', skills: [], taskCount: 0 }];
  const skillPath = 'C:/fixture/skills/desktop/SKILL.md';
  const extraSkills = Array.from({ length: 46 }, (_, index) => ({
    path: `C:/fixture/skills/extra-${index}/SKILL.md`, name: `extra-skill-${index}`,
    description: 'Individual selection fixture', category: 'Uncategorized',
  }));
  let skillCategory = 'Development';
  const bundlePath = path.join(home, 'skills.zip');
  const streams = new Set();
  let finishCancellation, cancellationStarted;
  const cancellationEntered = new Promise(resolve => { cancellationStarted = resolve; });
  let outsideRequests = 0;
  const outside = createServer(async (request, response) => {
    for await (const _chunk of request) { /* Never record the fixture payload. */ }
    outsideRequests++;
    response.end('{"leaked":true}');
  });
  await new Promise(resolve => outside.listen(0, '127.0.0.1', resolve));
  const server = createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk;
    try {
      const input = request.headers['signature-input'];
      const fields = input.slice(input.indexOf('(') + 1, input.indexOf(')')).split(' ').map(value => value.slice(1, -1));
      const nonce = input.match(/;nonce="([^"]+)"/)[1];
      const expectedFields = ['@method', '@path', '@query', 'content-digest', ...['content-type', 'idempotency-key', 'last-event-id'].filter(field => field in request.headers)];
      assert.deepEqual(fields, expectedFields);
      assert.ok(input.includes(`;keyid="${entry.id}";alg="ed25519"`));
      assert.ok(!nonces.has(nonce));
      assert.equal(request.headers['content-digest'], 'sha-256=:' + createHash('sha256').update(body).digest('base64') + ':');
      const question = request.url.indexOf('?');
      const values = { ...request.headers, '@method': request.method, '@path': question < 0 ? request.url : request.url.slice(0, question), '@query': question < 0 ? '?' : request.url.slice(question) };
      const base = fields.map(field => `"${field}": ${values[field]}`).join('\n') + '\n"@signature-params": ' + input.slice('permissive='.length);
      assert.ok(verify(null, Buffer.from(base), publicKey, Buffer.from(request.headers.signature.slice('permissive=:'.length, -1), 'base64')));
      nonces.add(nonce);
    } catch (error) { response.statusCode = 401; return response.end('Invalid public-key signature: ' + error.message); }
    response.setHeader('Content-Type', 'application/json');
    const route = new URL(request.url, 'http://localhost');
    if (request.url === '/v1/health') return response.end('{"ok":true}');
    if (request.url === '/v1/tasks' && request.method === 'POST' && body.includes('PRIVATE-REDIRECT-PROMPT')) {
      response.writeHead(307, { Location: `http://127.0.0.1:${outside.address().port}/outside?key=PRIVATE-KEY` });
      return response.end('PRIVATE-REDIRECT-RESPONSE');
    }
    if (route.pathname === '/v1/workspaces' && request.method === 'GET') return response.end(JSON.stringify({ data: workspaces }));
    if (route.pathname === '/v1/workspaces' && request.method === 'POST' || route.pathname.startsWith('/v1/workspaces/') && request.method === 'PATCH') {
      const values = JSON.parse(body);
      let workspace = workspaces.find(value => route.pathname.endsWith('/' + value.id));
      if (workspace) Object.assign(workspace, values); else { workspace = { id: 'workspace-fixture', taskCount: 0, ...values }; workspaces.push(workspace); }
      return response.end(JSON.stringify(workspace));
    }
    if (route.pathname === '/v1/skills') return response.end(JSON.stringify({ data: [{ path: skillPath, name: 'desktop-skill', description: 'Explicit workspace skill', category: skillCategory }, ...extraSkills] }));
    if (route.pathname === '/v1/skills/document') return response.end(JSON.stringify({ path: skillPath, name: 'desktop-skill', content: 'SKILL-PREVIEW-CONTENT' }));
    if (route.pathname === '/v1/skills/categories') { skillCategory = JSON.parse(body).category; return response.end('{"data":[]}'); }
    if (route.pathname === '/v1/skills/export') return response.end('ZIP-FIXTURE');
    if (route.pathname === '/v1/skills/import') return response.end(JSON.stringify({ data: [{ path: skillPath }] }));
    if (route.pathname === '/v1/browsers') {
      if (browserCatalogGate) await browserCatalogGate;
      return response.end(JSON.stringify({ data: [
      { id: 'chromium', name: 'Bundled Chromium', installed: true, profiles: ['work'] },
      { id: 'chrome', name: 'Google Chrome', installed: true, profiles: ['office'] },
      { id: 'msedge', name: 'Microsoft Edge', installed: true, profiles: [], installedProfiles: [
        { id: 'local-aaaaaaaaaaaaaaaaaaaaaaaa', name: 'Personal', account: 'personal@example.com', connected: connectedProfiles.has('local-aaaaaaaaaaaaaaaaaaaaaaaa') },
        { id: 'local-bbbbbbbbbbbbbbbbbbbbbbbb', name: 'Paul', account: 'work@example.com', connected: connectedProfiles.has('local-bbbbbbbbbbbbbbbbbbbbbbbb') },
      ] },
    ], connections: [{ connectionId: 'connected-edge', browser: 'msedge', profileId: 'edge-work', profileName: 'Work Edge' },
      ...[...connectedProfiles].map(localProfileId => ({ connectionId: localProfileId, browser: 'msedge', profileId: `extension-${localProfileId}`, localProfileId, profileName: 'Connected local profile' }))] }));
    }
    if (route.pathname === '/v1/browsers/connect') {
      assert.deepEqual(JSON.parse(body), { browser: 'msedge', profileId: 'local-bbbbbbbbbbbbbbbbbbbbbbbb' });
      return response.end(JSON.stringify({ setupRequired: true, extensionDirectory: 'C:/fixture/browser/extension' }));
    }
    if (route.pathname === '/v1/browsers/setup') {
      assert.deepEqual(JSON.parse(body), { browser: 'msedge', profileId: 'local-bbbbbbbbbbbbbbbbbbbbbbbb', confirmed: true });
      setupRequests++;
      connectedProfiles.add('local-bbbbbbbbbbbbbbbbbbbbbbbb');
      return response.end(JSON.stringify({ connected: true, extensionLoaded: true, paired: true }));
    }
    if (request.url === '/v1/tasks' && request.method === 'POST') {
      const input = JSON.parse(body);
      const workspace = workspaces.find(value => value.id === input.workspaceId);
      const submitted = { workingDirectory: workspace.workingDirectory, browserMode: workspace.browserMode, browser: workspace.browser, browserProfile: workspace.browserProfile, ...input };
      const id = submitted.prompt.includes('controls') ? 'desktop-controls' : tasks.length ? `desktop-fixture-${tasks.length}` : 'desktop-fixture';
      const task = { id, conversationId: id, threadId: `chat-${id}`, request: submitted,
        workspaceId: workspace.id,
        status: submitted.prompt.includes('controls') ? 'running' : 'completed',
        createdAt: Math.floor(Date.now() / 1000), result: submitted.prompt.includes('controls') ? null : 'DESKTOP-FIXTURE-OK', artifacts: [] };
      workspace.taskCount++; tasks.push(task); return response.end(JSON.stringify(task));
    }
    if (route.pathname === '/v1/tasks') return response.end(JSON.stringify({ data: tasks.filter(value => !route.searchParams.get('workspaceId') || value.workspaceId === route.searchParams.get('workspaceId')), nextCursor: null }));
    function chatEvents(chatTasks) {
      return chatTasks.flatMap(task => task.result ? [
        { id: tasks.indexOf(task) * 100 + 1, taskId: task.id, kind: 'agent', data: { method: 'item/completed', params: { item: { id: 'assistant', type: 'agentMessage', text: task.result } } } },
        { id: tasks.indexOf(task) * 100 + 2, taskId: task.id, kind: 'agent', data: { method: 'item/commandExecution/outputDelta', params: { itemId: 'command', delta: 'RAW-COMMAND-OUTPUT' } } },
        { id: tasks.indexOf(task) * 100 + 3, taskId: task.id, kind: 'agent', data: { method: 'item/reasoning/textDelta', params: { delta: 'RAW-REASONING' } } },
      ] : []);
    }
    if (route.pathname === '/v1/chats') {
      const filtered = tasks.filter(task => !route.searchParams.get('workspaceId') || task.workspaceId === route.searchParams.get('workspaceId'));
      return response.end(JSON.stringify({ data: [...new Map(filtered.map(task => [task.conversationId, { ...task, title: tasks.find(first => first.conversationId === task.conversationId).request.prompt }])).values()], nextCursor: null }));
    }
    if (route.pathname.startsWith('/v1/chats/')) {
      const id = route.pathname.split('/')[3], chatTasks = tasks.filter(task => task.conversationId === id), values = chatEvents(chatTasks);
      if (route.pathname.endsWith('/events')) {
        const filtered = values.filter(event => event.id > Number(route.searchParams.get('after') || 0));
        if (route.searchParams.get('format') === 'json') return response.end(JSON.stringify({ data: filtered }));
        response.setHeader('Content-Type', 'text/event-stream'); response.write(': connected\n\n');
        for (const event of filtered) response.write(`id: ${event.id}\nevent: agent\ndata: ${JSON.stringify(event)}\n\n`);
        streams.add(response); response.on('close', () => streams.delete(response)); return;
      }
      return response.end(JSON.stringify({ id, tasks: chatTasks, followups: [], queueHeld: false, eventCursor: values.at(-1)?.id || 0 }));
    }
    const selected = tasks.find(task => request.url.startsWith('/v1/tasks/' + task.id));
    if (request.url.endsWith('/continue')) {
      const task = { ...selected, id: `desktop-followup-${tasks.length}`, request: { ...selected.request, ...JSON.parse(body) }, status: 'completed', result: 'DESKTOP-FOLLOWUP-OK' };
      tasks.push(task); return response.end(JSON.stringify(task));
    }
    if (request.url.endsWith('/pause')) { selected.status = 'paused'; return response.end(JSON.stringify(selected)); }
    if (request.url.endsWith('/resume')) { selected.status = 'running'; return response.end(JSON.stringify(selected)); }
    if (request.url.endsWith('/cancel')) {
      selected.status = 'cancelling';
      await new Promise(resolve => { finishCancellation = resolve; cancellationStarted(); });
      selected.status = 'cancelled'; return response.end(JSON.stringify(selected));
    }
    if (request.url.startsWith('/v1/tasks/missing')) {
      response.statusCode = 404;
      return response.end('DESKTOP-MISSING-FIXTURE');
    }
    if (request.url.includes('/events')) {
      response.setHeader('Content-Type', 'text/event-stream');
      response.write(': connected\n\n');
      if (selected?.result) {
        const values = [
          { id: 1, kind: 'agent', data: { method: 'item/completed', params: { item: { id: 'assistant', type: 'agentMessage', text: selected.result } } } },
          { id: 2, kind: 'agent', data: { method: 'item/commandExecution/outputDelta', params: { delta: 'RAW-COMMAND-OUTPUT' } } },
          { id: 3, kind: 'agent', data: { method: 'item/reasoning/textDelta', params: { delta: 'RAW-REASONING' } } },
        ];
        for (const event of values) response.write(`id: ${event.id}\nevent: agent\ndata: ${JSON.stringify(event)}\n\n`);
      }
      streams.add(response);
      response.on('close', () => streams.delete(response));
      return;
    }
    response.end(JSON.stringify(selected || tasks[0]));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const desktop = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
  try {
    const page = await desktop.firstWindow();
    await page.getByText('● Service connected').waitFor();
    await page.getByRole('button', { name: 'New workspace', exact: true }).click();
    await page.getByLabel('Workspace name', { exact: true }).fill('Workbench');
    await page.getByLabel('Working directory', { exact: true }).fill(root);
    await page.getByLabel('System prompt', { exact: true }).fill('Use the Workbench conventions.');
    await page.getByLabel('Browser profile', { exact: true }).selectOption('__new');
    await page.getByLabel('New profile name', { exact: true }).fill('bad profile');
    assert.equal(await page.getByLabel('New profile name', { exact: true }).evaluate(element => element.checkValidity()), false);
    await page.getByLabel('New profile name', { exact: true }).fill('work-demo');
    assert.equal(await page.getByLabel('New profile name', { exact: true }).evaluate(element => element.checkValidity()), true);
    assert.equal(await page.getByRole('button', { name: 'Select all 46 skills in Uncategorized', exact: true }).isDisabled(), true);
    await page.getByRole('checkbox', { name: /desktop-skill/ }).check();
    await page.getByRole('checkbox', { name: /^extra-skill-0\s/ }).check();
    await page.getByRole('checkbox', { name: /^extra-skill-1\s/ }).check();
    assert.equal(await page.getByRole('checkbox').evaluateAll(elements => elements.filter(element => element.checked).length), 3);
    await page.getByText('3 of 32 skills selected.', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Clear selected skills', exact: true }).click();
    assert.equal(await page.getByRole('checkbox').evaluateAll(elements => elements.filter(element => element.checked).length), 0);
    await page.getByRole('checkbox', { name: /desktop-skill/ }).check();
    await page.getByRole('checkbox', { name: /^extra-skill-0\s/ }).check();
    await page.getByRole('checkbox', { name: /^extra-skill-1\s/ }).check();
    await page.getByRole('button', { name: 'Save workspace', exact: true }).click();
    await page.getByRole('heading', { name: 'What should we work on?' }).waitFor();
    assert.equal(workspaces[1].systemPrompt, 'Use the Workbench conventions.');
    assert.deepEqual(workspaces[1].skills, [skillPath, extraSkills[0].path, extraSkills[1].path]);
    assert.equal(workspaces[1].browserProfile, 'work-demo');
    await page.getByRole('button', { name: 'Skills', exact: true }).click();
    await page.getByRole('button', { name: 'View skill desktop-skill' }).click();
    await page.getByText('SKILL-PREVIEW-CONTENT', { exact: true }).waitFor();
    await page.getByRole('checkbox', { name: /desktop-skill/ }).check();
    await page.getByLabel('Set category', { exact: true }).fill('Operations');
    await page.getByRole('button', { name: 'Apply category', exact: true }).click();
    await page.getByText('Category saved.', { exact: true }).waitFor();
    assert.equal(skillCategory, 'Operations');
    await page.getByLabel('Filter by category').selectOption('Operations');
    desktop.configureDialogs({ saveFile: bundlePath, openFiles: [bundlePath] });
    await page.getByRole('button', { name: 'Export selected skills…', exact: true }).click();
    await page.getByRole('status').filter({ hasText: 'Skills exported to' }).waitFor();
    assert.equal(readFileSync(bundlePath, 'utf8'), 'ZIP-FIXTURE');
    await page.getByRole('button', { name: 'Import skills…', exact: true }).click();
    await page.getByText('1 skill imported.', { exact: true }).waitFor();
    assert.ok((await page.locator('.skill-list').boundingBox()).height > 100);
    await page.screenshot({ path: 'C:/codex-build/permissive-skills-library.png' });
    await page.getByRole('button', { name: 'Close library', exact: true }).click();
    await page.getByRole('button', { name: 'Chat options', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Browser and profile', exact: true }).click();
    await page.getByLabel('Browser', { exact: true }).selectOption('chrome');
    await page.getByLabel('Browser profile', { exact: true }).selectOption('office');
    await page.getByRole('button', { name: 'Close chat options', exact: true }).click();
    await page.getByRole('button', { name: 'Chat options', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Skills', exact: true }).click();
    await page.getByRole('button', { name: 'Clear chat skills', exact: true }).click();
    await page.getByRole('checkbox', { name: /desktop-skill/ }).check();
    await page.getByRole('button', { name: 'Close chat options', exact: true }).click();
    await page.getByRole('button', { name: 'Chat options', exact: true }).click();
    await page.getByRole('menuitemradio', { name: 'Plan', exact: true }).click();
    await page.getByLabel('Task prompt').fill('Exercise standalone desktop');
    await page.getByRole('button', { name: 'Plan task', exact: true }).click();
    await page.getByText('DESKTOP-FIXTURE-OK', { exact: true }).waitFor();
    assert.equal(tasks.length, 1);
    assert.equal(tasks[0].workspaceId, 'workspace-fixture');
    assert.equal(tasks[0].request.browser, 'chrome');
    assert.equal(tasks[0].request.browserProfile, 'office');
    assert.equal(tasks[0].request.mode, 'plan');
    assert.deepEqual(tasks[0].request.skills, [skillPath]);
    assert.equal(workspaces[1].browser, 'chromium');
    assert.equal(await page.evaluate(() => typeof window.require), 'undefined');
    assert.equal(await page.evaluate(() => typeof window.process), 'undefined');
    const forbidden = await page.evaluate(async target => {
      try { await fetch(target, { method: 'POST', body: 'PRIVATE-PROMPT' }); return false; } catch { return true; }
    }, `http://127.0.0.1:${server.address().port}/outside`);
    assert.equal(forbidden, true, 'The renderer must not send requests directly');
    const denied = await page.evaluate(async () => {
      try { await window.__TAURI_INTERNALS__.invoke('desktop_call', { operation: 'arbitrary-process', args: ['PRIVATE-PROMPT'] }); } catch (error) { return String(error); }
    });
    assert.match(denied, /Unsupported desktop operation/);
    await page.getByRole('button', { name: 'Activity', exact: true }).click();
    await page.getByText('Execution details (1 event)', { exact: true }).waitFor();
    assert.equal(await page.locator('.execution-details').getAttribute('open'), null);
    assert.equal(await page.getByText('RAW-COMMAND-OUTPUT', { exact: true }).isVisible(), false);
    await page.getByText('Execution details (1 event)', { exact: true }).click();
    assert.equal(await page.getByText('RAW-COMMAND-OUTPUT', { exact: true }).isVisible(), true);
    await page.getByRole('dialog', { name: 'Activity', exact: true }).getByRole('button', { name: 'Close', exact: true }).click();
    desktop.configureDialogs({ menuAction: 'copy-chat' });
    await page.locator('.task').first().click({ button: 'right' });
    await page.waitForTimeout(100);
    assert.equal(execFileSync('powershell.exe', ['-NoProfile', '-Command', 'Get-Clipboard -Raw'], { encoding: 'utf8', windowsHide: true }).trim(), 'chat-desktop-fixture');
    await page.getByRole('button', { name: 'Chat options', exact: true }).click();
    await page.getByRole('menuitemradio', { name: 'Goal', exact: true }).click();
    await page.getByLabel('Goal objective', { exact: true }).fill('Finish this chat goal');
    await page.getByRole('button', { name: 'Close chat options', exact: true }).click();
    await page.getByLabel('Task prompt').fill('Follow up in this chat');
    await page.getByRole('button', { name: 'Continue conversation', exact: true }).click();
    await page.getByText('DESKTOP-FOLLOWUP-OK', { exact: true }).waitFor();
    assert.equal(tasks[1].request.mode, 'goal');
    assert.equal(tasks[1].request.goal, 'Finish this chat goal');
    assert.equal(tasks[1].request.browserProfile, 'office');
    assert.equal(tasks[1].threadId, tasks[0].threadId);
    assert.equal(await page.locator('.task').count(), 1);
    assert.equal(await page.getByText('DESKTOP-FIXTURE-OK', { exact: true }).count(), 1);
    await page.getByRole('button', { name: 'Chat options', exact: true }).click();
    await page.screenshot({ path: 'C:/codex-build/permissive-chat-plus-menu.png' });
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: /^General\s/ }).click();
    assert.equal(await page.locator('.task').count(), 0);
    await page.getByRole('button', { name: /^Workbench\s/ }).click();
    await page.locator('.task').first().waitFor();
    await page.getByRole('button', { name: 'Workspace settings for Workbench', exact: true }).click();
    assert.equal(await page.getByLabel('System prompt', { exact: true }).inputValue(), 'Use the Workbench conventions.');
    assert.equal(await page.getByRole('checkbox', { name: /desktop-skill/ }).isChecked(), true);
    await page.getByLabel('Workspace name', { exact: true }).fill('Workbench renamed');
    await page.getByLabel('Browser mode', { exact: true }).selectOption('attached');
    await page.getByLabel('Browser', { exact: true }).selectOption('msedge');
    assert.match((await page.getByLabel('Browser profile', { exact: true }).locator('option').allTextContents()).join('\n'), /personal@example.com/);
    await page.getByLabel('Browser profile', { exact: true }).selectOption('local-bbbbbbbbbbbbbbbbbbbbbbbb');
    desktop.configureDialogs({ confirm: false });
    await page.getByRole('button', { name: 'Enable and connect', exact: true }).click();
    await page.getByRole('button', { name: 'Enable and connect', exact: true }).waitFor();
    assert.equal(setupRequests, 0);
    desktop.configureDialogs({ confirm: true });
    await page.getByRole('button', { name: 'Enable and connect', exact: true }).click();
    await page.getByRole('status').filter({ hasText: 'Browser profile connected. Ready to use in this chat.' }).waitFor();
    assert.equal(setupRequests, 1);
    await page.getByLabel('Browser profile', { exact: true }).selectOption('edge-work');
    await page.locator('.browser-settings').scrollIntoViewIfNeeded();
    await page.screenshot({ path: 'C:/codex-build/permissive-browser-settings.png' });
    await page.getByRole('button', { name: 'Save workspace', exact: true }).click();
    assert.equal(workspaces[1].name, 'Workbench renamed');
    assert.equal(workspaces[1].browser, 'msedge');
    assert.equal(workspaces[1].browserProfile, 'edge-work');
    const cancelled = await page.evaluate(async () => {
      const pending = window.permissive.watch('desktop-fixture');
      await new Promise(resolve => setTimeout(resolve, 100));
      await window.permissive.unwatch();
      return pending;
    });
    assert.deepEqual(cancelled, { cancelled: true });
    await page.getByRole('button', { name: 'New task' }).click();
    await page.getByRole('heading', { name: 'What should we work on?' }).waitFor();
    const browserChip = page.getByRole('button', { name: 'Change chat browser', exact: true });
    await page.waitForFunction(() => document.querySelector('[aria-label="Change chat browser"]').textContent === 'Edge · Work Edge · Connected');
    await page.getByRole('button', { name: 'Change chat browser', exact: true }).click();
    assert.equal(await page.getByLabel('Browser', { exact: true }).inputValue(), 'msedge');
    assert.equal(await page.getByLabel('Browser profile', { exact: true }).inputValue(), 'edge-work');
    await page.getByLabel('Browser profile', { exact: true }).selectOption('');
    await page.waitForFunction(() => document.querySelector('[aria-label="Change chat browser"]').textContent === 'Edge · Any profile · Choose a profile');
    let releaseBrowserCatalog;
    browserCatalogGate = new Promise(resolve => { releaseBrowserCatalog = resolve; });
    await page.getByLabel('Browser profile', { exact: true }).selectOption('local-aaaaaaaaaaaaaaaaaaaaaaaa');
    assert.match(await browserChip.innerText(), /Checking…$/);
    releaseBrowserCatalog(); browserCatalogGate = null;
    await page.waitForFunction(() => document.querySelector('[aria-label="Change chat browser"]').textContent === 'Edge · personal@example.com · Not connected');
    // An unrelated connected Edge profile must not make the selected profile look ready.
    await page.getByRole('button', { name: 'Close chat options', exact: true }).click();
    connectedProfiles.add('local-aaaaaaaaaaaaaaaaaaaaaaaa');
    await page.waitForFunction(() => document.querySelector('[aria-label="Change chat browser"]').textContent === 'Edge · personal@example.com · Connected');
    connectedProfiles.delete('local-aaaaaaaaaaaaaaaaaaaaaaaa');
    await page.waitForFunction(() => document.querySelector('[aria-label="Change chat browser"]').textContent === 'Edge · personal@example.com · Not connected');
    await browserChip.click();
    await page.getByLabel('Browser profile', { exact: true }).selectOption('edge-work');
    await page.getByRole('button', { name: 'Close chat options', exact: true }).click();
    await page.getByLabel('Task prompt').fill('Exercise task controls');
    await page.getByRole('button', { name: 'Run task' }).click();
    await page.getByRole('button', { name: 'Pause task' }).click();
    await page.getByText('Paused. Select Resume task to continue.', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Resume task' }).click();
    await page.getByRole('button', { name: 'Pause task' }).waitFor();
    await page.getByRole('button', { name: 'Cancel task' }).click();
    assert.equal(await page.getByRole('button', { name: 'Cancelling…', exact: true }).isDisabled(), true);
    await cancellationEntered;
    finishCancellation();
    await page.getByRole('status').filter({ hasText: 'Cancelled' }).waitFor();
    await page.getByRole('button', { name: 'New task' }).click();
    await page.evaluate(() => {
      console.error('PERMISSIVE-CLOSED-PIPE-REGRESSION');
      console.log('PRIVATE-PROMPT', new Error('PRIVATE-KEY'));
    });
    const ipcError = await page.evaluate(async () => {
      try { await window.permissive.watch('missing'); }
      catch (error) { return error.message; }
    });
    assert.match(ipcError, /HTTP 404/);
    const redirectError = await page.evaluate(async () => {
      try { await window.permissive.submit({ prompt: 'PRIVATE-REDIRECT-PROMPT' }); }
      catch (error) { return error.message; }
    });
    assert.equal(redirectError, 'API operation failed (HTTP 307)');
    assert.equal(outsideRequests, 0, 'Signed prompt requests must never follow API redirects.');
    await page.getByRole('button', { name: 'Run task' }).waitFor();
    assert.equal(desktop.process().exitCode, null);
    const log = readFileSync(path.join(home, 'desktop.log'), 'utf8');
    assert.doesNotMatch(log, /PERMISSIVE-CLOSED-PIPE-REGRESSION|DESKTOP-MISSING-FIXTURE|closed stdout fixture|closed stderr fixture/);
    assert.doesNotMatch(log, /PRIVATE-/);
    assert.match(log, /desktop.started/);
    assert.match(log, /service.ready/);
    await page.screenshot({ path: 'C:/codex-build/permissive-desktop.png' });
  } catch (error) {
    await (await desktop.firstWindow()).screenshot({ path: 'C:/codex-build/permissive-workspaces-desktop-failure.png' });
    throw error;
  } finally {
    finishCancellation?.();
    await desktop.close();
    for (const response of streams) response.end();
    await new Promise(resolve => server.close(resolve));
    await new Promise(resolve => outside.close(resolve));
    assert.equal(path.dirname(realpathSync(home)), realpathSync(tmpdir()));
    rmSync(home, { recursive: true, force: true });
  }
});
