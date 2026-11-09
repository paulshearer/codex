import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { randomUUID, generateKeyPairSync, privateDecrypt, createDecipheriv, constants } from 'node:crypto';
import { launchDesktop } from './desktop-harness.mjs';

test('real desktop seals credentials before IPC and supports timed grants and revocation', { timeout: 120000 }, async () => {
  const root = process.cwd(), python = 'C:/codex-build/permissive-service-venv/Scripts/python.exe';
  const home = mkdtempSync(path.join(tmpdir(), 'permissive-credentials-ui-'));
  execFileSync(python, ['-m', 'permissive.auth', '--home', home, 'init-local', '--ensure'], { env: { ...process.env, PYTHONPATH: root }, stdio: 'ignore' });
  const workspace = { id: randomUUID(), name: 'General', workingDirectory: root, skills: [], browserMode: 'dedicated', browser: 'chromium', taskCount: 0 };
  const entries = new Map(), pending = new Map(), bodies = [];
  const username = 'synthetic-user-用户', password = 'synthetic-password-秘密';
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    bodies.push(raw);
    assert.ok(!raw.includes(username)); assert.ok(!raw.includes(password));
    assert.ok(request.headers.signature);
    const body = raw ? JSON.parse(raw) : null, url = new URL(request.url, 'http://localhost');
    response.setHeader('Content-Type', 'application/json');
    const send = value => response.end(JSON.stringify(value));
    if (url.pathname === '/v1/health') return send({ ok: true });
    if (url.pathname === '/v1/workspaces') return send({ data: [workspace] });
    if (url.pathname === '/v1/chats') return send({ data: [], nextCursor: null });
    if (url.pathname.startsWith('/v1/browsers') || url.pathname.startsWith('/v1/skills')) return send({ data: [], connections: [] });
    if (url.pathname === '/v1/credentials/prepare') {
      assert.equal(body.workspaceId, workspace.id);
      const key = generateKeyPairSync('rsa', { modulusLength: 2048 }), uploadId = randomUUID();
      pending.set(uploadId, key.privateKey);
      return send({ uploadId, publicKey: key.publicKey.export({ type: 'spki', format: 'der' }).toString('base64'), algorithm: 'RSA-OAEP-256/A256GCM', expiresAt: Math.floor(Date.now() / 1000) + 120 });
    }
    if (url.pathname === '/v1/credentials' && request.method === 'POST') {
      const key = privateDecrypt({ key: pending.get(body.uploadId), oaepHash: 'sha256', padding: constants.RSA_PKCS1_OAEP_PADDING }, Buffer.from(body.wrappedKey, 'base64'));
      pending.delete(body.uploadId);
      const data = Buffer.from(body.ciphertext, 'base64');
      const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(body.iv, 'base64'));
      decipher.setAAD(Buffer.from(body.uploadId + '\n' + workspace.id)); decipher.setAuthTag(data.subarray(-16));
      const payload = JSON.parse(Buffer.concat([decipher.update(data.subarray(0, -16)), decipher.final()]).toString('utf8'));
      assert.equal(payload.username, username); assert.equal(payload.password, password);
      assert.equal(payload.hours, 2); assert.equal(payload.lifetime, 'hours');
      const metadata = { id: body.uploadId, workspaceId: workspace.id, label: payload.label, origin: payload.origin, lifetime: 'hours', expiresAt: Math.floor(Date.now() / 1000) + 7200 };
      entries.set(body.uploadId, metadata); return send(metadata);
    }
    if (url.pathname.startsWith('/v1/credentials') && request.method === 'DELETE') {
      assert.equal(url.searchParams.get('workspaceId'), workspace.id);
      const id = url.pathname.split('/')[3]; if (id) entries.delete(id); else entries.clear();
      return send({ revoked: true });
    }
    if (url.pathname === '/v1/credentials') return send({ data: [...entries.values()] });
    response.statusCode = 404; send({});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await launchDesktop({ root, home, python, apiUrl: `http://127.0.0.1:${server.address().port}` });
    const page = await app.firstWindow();
    await page.getByText('● Service connected').waitFor();
    await page.locator('.workspace.active').waitFor();
    await page.getByLabel('Chat options', { exact: true }).click();
    await page.getByRole('menuitem', { name: 'Secure credentials', exact: true }).click();
    const panel = page.getByRole('dialog', { name: 'Secure credentials', exact: true });
    await panel.getByLabel('Credential label').fill('Work account');
    await panel.getByLabel('Credential website origin').fill('https://example.com');
    await panel.getByLabel('Credential username').fill(username);
    await panel.getByLabel('Credential password').fill(password);
    await panel.getByLabel('Credential lifetime').selectOption('hours');
    await panel.getByLabel('Credential hours').fill('2');
    await panel.getByRole('button', { name: 'Provide securely' }).click();
    await panel.locator('.credential-entry').getByText('Work account', { exact: true }).waitFor();
    assert.equal(entries.size, 1);
    assert.equal(await panel.getByLabel('Credential password').inputValue(), '');
    assert.equal(await panel.getByLabel('Credential username').inputValue(), '');
    const persisted = await page.evaluate(() => JSON.stringify({ ...localStorage }));
    assert.ok(!persisted.includes(password)); assert.ok(!persisted.includes(username));
    await page.screenshot({ path: 'C:/codex-build/permissive-secure-credentials.png' });
    await panel.getByRole('button', { name: 'Revoke', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.credential-entry'));
    assert.equal(entries.size, 0);
    await panel.getByRole('button', { name: 'Close', exact: true }).click();
    assert.equal(await panel.count(), 0);
    assert.ok(bodies.some(raw => raw.includes('wrappedKey')));
  } finally {
    if (app) await app.close();
    await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(home), tmpdir()); rmSync(home, { recursive: true, force: true });
  }
});
