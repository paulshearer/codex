import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readdir, readFile, writeFile, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { launchPreferences } from '../browser/preferences.mjs';

test('browser preferences choose channels and isolate named profiles by browser', () => {
  const env = { PERMISSIVE_HOME: 'C:/fixture', PERMISSIVE_TASK_ID: 'task', PERMISSIVE_BROWSER_SESSION_ID: 'conversation' };
  assert.equal(launchPreferences(env).options.channel, undefined);
  assert.equal(launchPreferences(env).directory, path.join(env.PERMISSIVE_HOME, 'browsers', 'conversation'));
  for (const browser of ['chrome', 'msedge']) {
    const value = launchPreferences({ ...env, PERMISSIVE_BROWSER: browser, PERMISSIVE_BROWSER_PROFILE: 'work' });
    assert.equal(value.options.channel, browser);
    assert.equal(value.directory, path.join(env.PERMISSIVE_HOME, 'browser-profiles', browser, 'work'));
  }
  assert.throws(() => launchPreferences({ ...env, PERMISSIVE_BROWSER_PROFILE: '../escape' }));
  assert.throws(() => launchPreferences({ ...env, PERMISSIVE_BROWSER_PROFILE: 'CON' }));
});

for (const browser of ['chrome', 'msedge']) {
  const vendor = browser === 'chrome' ? 'Google/Chrome/Application' : 'Microsoft/Edge/Application';
  const executable = browser === 'chrome' ? 'chrome.exe' : 'msedge.exe';
  const installed = ['LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'].some(key => process.env[key] && existsSync(path.join(process.env[key], vendor, executable)));
  test(`dedicated ${browser} launches with a managed profile`, { skip: !installed }, async () => {
    const home = await mkdtemp(path.join(os.tmpdir(), `permissive-${browser}-`));
    let context;
    try {
      const preferences = launchPreferences({ PERMISSIVE_HOME: home, PERMISSIVE_BROWSER: browser, PERMISSIVE_BROWSER_PROFILE: 'work', PERMISSIVE_HEADLESS: '1' });
      context = await chromium.launchPersistentContext(preferences.directory, preferences.options);
      const page = await context.newPage();
      await page.goto('data:text/html,<title>Managed browser fixture</title><p>Browser channel works</p>');
      assert.equal(await page.title(), 'Managed browser fixture');
    } finally {
      await context?.close();
      await rm(home, { recursive: true, force: true });
    }
  });
}

test('dedicated browser navigates, edits, inspects, and produces screenshot artifacts', async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), 'permissive-browser-'));
  const server = createServer((request, response) => {
    if (request.url === '/download') {
      response.setHeader('Content-Type', 'application/octet-stream');
      response.setHeader('Content-Disposition', 'attachment; filename="fixture.txt"');
      return response.end('downloaded fixture');
    }
    response.end('<html><body><input id="name"><input id="file" type="file"><a id="download" href="/download">Download</a><button id="go" onclick="document.querySelector(\'p\').textContent=document.querySelector(\'input\').value">Save</button><p>before</p></body></html>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const pending = new Map(); let sequence = 0;
  let child;
  function start(taskId, profile = '', session = 'conversation-fixture') {
    child = spawn(process.execPath, ['browser/worker.mjs'], { env: { ...process.env, PERMISSIVE_HOME: home,
      PERMISSIVE_TASK_ID: taskId, PERMISSIVE_BROWSER_SESSION_ID: session, PERMISSIVE_BROWSER_PROFILE: profile, PERMISSIVE_HEADLESS: '1' }, stdio: ['pipe', 'pipe', 'inherit'] });
    readline.createInterface({ input: child.stdout }).on('line', line => { const message = JSON.parse(line); pending.get(message.id)?.(message); });
  }
  start('fixture');
  async function call(method, params) {
    const id = ++sequence;
    const result = new Promise(resolve => pending.set(id, resolve));
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    const message = await result;
    assert.equal(message.error, undefined);
    assert.equal(message.result.isError, undefined, JSON.stringify(message.result));
    return message.result;
  }
  async function operation(arguments_) {
    const result = await call('tools/call', { name: 'browser', arguments: arguments_ });
    assert.ok(Buffer.byteLength(result.content[0].text) <= 65536);
    return result.content[0].text;
  }
  try {
    await call('initialize', { protocolVersion: '2024-11-05' });
    const page = JSON.parse(await operation({ operation: 'open', url: `http://127.0.0.1:${server.address().port}` }));
    await operation({ operation: 'type', pageId: page.pageId, selector: '#name', text: 'saved fixture' });
    await operation({ operation: 'click', pageId: page.pageId, selector: '#go' });
    assert.match(await operation({ operation: 'inspect', pageId: page.pageId }), /saved fixture/);
    const snapshot = JSON.parse(await operation({ operation: 'inspect', pageId: page.pageId }));
    const save = snapshot.elements.find(element => element.name === 'Save');
    assert.ok(save.selector);
    await operation({ operation: 'click', pageId: page.pageId, selector: save.selector });
    await operation({ operation: 'evaluate', pageId: page.pageId, script: 'document.body.appendChild(Object.assign(document.createElement("p"),{id:"long-message",textContent:"Mail toolbar ".repeat(400)+"Complete latest message body: café 日本語 🚀"}))' });
    const long = JSON.parse(await operation({ operation: 'inspect', pageId: page.pageId }));
    assert.match(long.text, /Complete latest message body: café 日本語 🚀/);
    assert.equal(long.nextOffset, null);
    const scoped = JSON.parse(await operation({ operation: 'inspect', pageId: page.pageId, selector: '#long-message', limit: 2000 }));
    assert.equal(scoped.nextOffset, 2000);
    assert.equal(scoped.text.length, 2000);
    await operation({ operation: 'wait', pageId: page.pageId, selector: '#long-message' });
    const upload = path.join(home, 'upload.txt');
    await writeFile(upload, 'uploaded fixture');
    await operation({ operation: 'upload', pageId: page.pageId, selector: '#file', file: upload });
    assert.match(await operation({ operation: 'evaluate', pageId: page.pageId, script: 'document.querySelector("#file").files[0].name' }), /upload.txt/);
    const download = JSON.parse(await operation({ operation: 'download', pageId: page.pageId, selector: '#download' }));
    assert.equal(await readFile(path.join(home, 'artifacts', 'fixture', download.artifactId), 'utf8'), 'downloaded fixture');
    await operation({ operation: 'artifact', file: upload });
    await operation({ operation: 'screenshot', pageId: page.pageId });
    assert.equal((await readdir(path.join(home, 'artifacts', 'fixture'))).length, 3);
    const large = JSON.parse(await operation({ operation: 'evaluate', pageId: page.pageId, script: '"large complete response 日本語".repeat(10000)' }));
    assert.equal(large.truncated, true);
    assert.equal(JSON.parse(await readFile(large.path, 'utf8')), 'large complete response 日本語'.repeat(10000));
    await operation({ operation: 'evaluate', pageId: page.pageId, script: 'document.cookie="profileFixture=persisted; max-age=3600; path=/"' });
    child.stdin.end();
    await new Promise(resolve => child.once('exit', resolve));
    start('successor');
    await call('initialize', { protocolVersion: '2024-11-05' });
    const continuedPage = JSON.parse(await operation({ operation: 'open', url: `http://127.0.0.1:${server.address().port}` }));
    assert.match(await operation({ operation: 'evaluate', pageId: continuedPage.pageId, script: 'document.cookie' }), /profileFixture=persisted/);
    child.stdin.end();
    await new Promise(resolve => child.once('exit', resolve));
    start('named-first', 'work', 'first-conversation');
    const named = JSON.parse(await operation({ operation: 'open', url: `http://127.0.0.1:${server.address().port}` }));
    assert.doesNotMatch(await operation({ operation: 'evaluate', pageId: named.pageId, script: 'document.cookie' }), /profileFixture/);
    await operation({ operation: 'evaluate', pageId: named.pageId, script: 'document.cookie="namedProfile=work; max-age=3600; path=/"' });
    child.stdin.end();
    await new Promise(resolve => child.once('exit', resolve));
    start('named-second', 'work', 'different-conversation');
    const shared = JSON.parse(await operation({ operation: 'open', url: `http://127.0.0.1:${server.address().port}` }));
    assert.match(await operation({ operation: 'evaluate', pageId: shared.pageId, script: 'document.cookie' }), /namedProfile=work/);
  } finally {
    child.stdin.end();
    await new Promise(resolve => child.once('exit', resolve));
    server.close();
    await rm(home, { recursive: true, force: true });
  }
});
