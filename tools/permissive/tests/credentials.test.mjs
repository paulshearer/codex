import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const fillSource = await readFile(new URL('../browser/extension/credential-fill.js', import.meta.url), 'utf8');
const domSource = await readFile(new URL('../browser/extension/dom.js', import.meta.url), 'utf8');

test('secure fill checks origin, expiry, input kind and form destination, then submits a real sign-in form', async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  let submitted;
  await context.route('https://login.example/**', route => {
    if (route.request().method() === 'POST') {
      submitted = new URLSearchParams(route.request().postData());
      return route.fulfill({ contentType: 'text/html; charset=utf-8', body: 'Signed in' });
    }
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body:
      '<form method="post"><input id="user" name="username"><input id="pass" name="password" type="password"><input id="hidden" type="password" hidden><textarea id="text"></textarea></form>' });
  });
  const page = await context.newPage();
  try {
    await page.goto('https://login.example/');
    await page.addScriptTag({ content: domSource });
    await page.addScriptTag({ content: fillSource });
    const payload = { origin: 'https://login.example', selector: '#pass', field: 'password', value: 'fixture-secret-秘密', expiresAt: null };
    for (const altered of [{ origin: 'https://wrong.example' }, { expiresAt: 1 }, { selector: '#user' }, { selector: '#hidden' }, { selector: 'input' }, { selector: '#text' }]) {
      await assert.rejects(page.evaluate(value => permissiveCredentialFill(value), { ...payload, ...altered }));
      assert.equal(await page.locator('#pass').inputValue(), '');
    }
    await page.locator('form').evaluate(form => form.action = 'https://wrong.example/login');
    await assert.rejects(page.evaluate(value => permissiveCredentialFill(value), payload));
    await page.locator('form').evaluate(form => form.action = '/login');
    assert.deepEqual(await page.evaluate(value => permissiveCredentialFill(value), payload), { ok: true });
    assert.equal(await page.locator('#pass').inputValue(), payload.value);
    await page.evaluate(value => permissiveCredentialFill(value), { ...payload, selector: '#user', field: 'username', value: 'fixture-user' });
    assert.equal(await page.locator('#user').inputValue(), 'fixture-user');
    await Promise.all([page.waitForURL('https://login.example/login'), page.locator('form').evaluate(form => form.requestSubmit())]);
    assert.equal(submitted.get('username'), 'fixture-user');
    assert.equal(submitted.get('password'), payload.value);
  } finally { await browser.close(); }
});

test('worker returns only success and sanitizes credential-bearing browser errors in both modes', async () => {
  const source = await readFile(new URL('../browser/worker.mjs', import.meta.url), 'utf8');
  const operationSource = source.slice(source.indexOf('async function credentialOperation'), source.indexOf('const input = readline'));
  for (const attached of [false, true]) {
    let filled;
    const secret = { origin: 'https://example.com', username: 'fixture-user', password: 'secret-秘密', expiresAt: null };
    const context = { attached, sign: () => ({}), taskId: 'task', AbortSignal, JSON, URL,
      bounded: value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
      browser: async () => {},
      pages: new Map([['1', { isClosed: () => false, url: () => 'https://example.com/login', evaluate: async expr => {
        if (expr.args.operation === 'credentialContext') return { origin: 'https://example.com', formOrigins: ['https://example.com'] };
        filled = expr.args;
      } }]]),
      permissiveExpression: (fn, args) => ({ fn, args }), permissiveAction: () => {},
      permissiveCredentialFill: () => {},
      fetch: async () => ({ ok: true, json: async () => secret }),
      attachedCall: async args => {
        if (args.operation === 'credentialContext') return { content: [{ text: JSON.stringify({ result: { origin: 'https://example.com', formOrigins: ['https://example.com'] } }) }] };
        filled = args.payload; return { content: [{ text: '{"ok":true}' }] };
      },
    };
    const run = runInNewContext(operationSource + '; credentialOperation', context);
    const args = { operation: 'fillCredential', credentialId: 'credential', pageId: '1', tabId: 3, selector: '#pass', field: 'password' };
    const result = await run(args);
    assert.equal(filled.value, secret.password);
    assert.deepEqual(JSON.parse(result.content[0].text), { ok: true });
    assert.ok(!JSON.stringify(result).includes(secret.password));
    if (attached) context.attachedCall = async () => { throw new Error(secret.password); };
    else context.pages.get('1').evaluate = async () => { throw new Error(secret.password); };
    await assert.rejects(run(args), error => error.message.startsWith('Secure fill failed.') && !error.message.includes(secret.password));
  }
});

test('redirected sign-in components use inspected refs and validate form destinations at fill time', async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  let submitted;
  await context.route('**/*', route => route.fulfill({ status: 500, body: 'Unexpected fixture request' }));
  await context.route('https://www.aa.com/**', route => route.fulfill({ contentType: 'text/html', body: '<script>location.replace("https://login.aa.com/login")</script>' }));
  await context.route('https://login.aa.com/**', route => route.fulfill({ contentType: 'text/html', body: '<aa-login></aa-login>' }));
  await context.route('https://auth.aa.com/**', route => { submitted = new URLSearchParams(route.request().postData()); return route.fulfill({ body: 'Fixture signed in' }); });
  const page = await context.newPage();
  const inspection = await readFile(new URL('../browser/extension/inspection.js', import.meta.url), 'utf8');
  try {
    await page.goto('https://www.aa.com/');
    await page.waitForURL('https://login.aa.com/login');
    await page.locator('aa-login').waitFor({ state: 'attached' });
    await page.evaluate(() => {
      const shadow = document.querySelector('aa-login').attachShadow({ mode: 'open' });
      shadow.innerHTML = '<form method="post" action="https://auth.aa.com/session"><label>Account<input name="username"></label><label>Password<input name="password" type="password"></label><button>Log in</button></form>';
    });
    await page.addScriptTag({ content: domSource + '\n' + inspection + '\n' + fillSource });
    const snapshot = await page.evaluate(() => permissiveInspect());
    const user = snapshot.elements.find(item => item.name === 'Account');
    const pass = snapshot.elements.find(item => item.name === 'Password');
    assert.ok(user.ref); assert.ok(pass.ref);
    const preflight = await page.evaluate(ref => permissiveAction({ operation: 'credentialContext', selector: ref, field: 'password' }), pass.ref);
    assert.equal(preflight.origin, 'https://login.aa.com');
    assert.deepEqual(preflight.formOrigins, ['https://login.aa.com', 'https://auth.aa.com']);
    const payload = { ...preflight, expiresAt: Date.now() / 1000 + 15, selector: pass.ref, field: 'password', value: 'fixture-private-password' };
    await page.evaluate(() => document.querySelector('aa-login').shadowRoot.querySelector('form').action = 'https://aa.com.evil.com/');
    await assert.rejects(page.evaluate(value => permissiveCredentialFill(value), payload), /destination changed/);
    await page.evaluate(() => document.querySelector('aa-login').shadowRoot.querySelector('form').action = 'https://auth.aa.com/session');
    assert.deepEqual(await page.evaluate(value => permissiveCredentialFill(value), payload), { ok: true });
    await page.evaluate(value => permissiveCredentialFill(value), { ...payload, selector: user.ref, field: 'username', value: 'fixture-account' });
    assert.ok(!JSON.stringify(await page.evaluate(() => permissiveInspect())).includes(payload.value));
    const point = await page.evaluate(ref => permissiveAction({ operation: 'clickPoint', selector: ref }), snapshot.elements.find(item => item.name === 'Log in').ref);
    await Promise.all([page.waitForURL('https://auth.aa.com/session'), page.mouse.click(point.x, point.y)]);
    assert.equal(submitted.get('username'), 'fixture-account');
    assert.equal(submitted.get('password'), payload.value);
  } finally { await browser.close(); }
});
