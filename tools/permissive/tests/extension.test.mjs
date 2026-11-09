import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import os from 'node:os';
import path from 'node:path';

test('connecting an existing native port announces its current paired profile', async () => {
  const stored = { connectEnabled: true, profileId: 'extension-profile', profileName: 'Work profile', localProfileId: 'local-' + 'a'.repeat(24) };
  let onMessage;
  const messages = [];
  const ignore = () => {};
  const chrome = {
    storage: { local: { get: async () => stored, set: async value => Object.assign(stored, value) }, onChanged: { addListener: ignore } },
    action: { setBadgeText: ignore, onClicked: { addListener: ignore } },
    runtime: { id: 'fixture', onMessage: { addListener: callback => { onMessage = callback; } },
      connectNative() { throw new Error('An existing native port must be reused'); } },
    debugger: { onDetach: { addListener: ignore } },
    tabs: { onRemoved: { addListener: ignore } },
  };
  const source = await readFile(new URL('../browser/extension/background.js', import.meta.url), 'utf8');
  runInNewContext(source + '\nport = { postMessage: message => messages.push(message) };',
    { chrome, navigator: { userAgent: 'Edg/' }, importScripts: ignore, setTimeout, clearTimeout, messages });
  // No storage change event occurs when Save and connect repeats the same binding.
  onMessage({ operation: 'connect' }, { id: 'fixture' }, ignore);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.ok(messages.length > 0);
  assert.equal(messages.at(-1).operation, 'hello');
  assert.equal(messages.at(-1).localProfileId, stored.localProfileId);
  assert.equal(messages.at(-1).profileId, stored.profileId);
  assert.equal(messages.at(-1).browser, 'msedge');
  const expired = runInNewContext(source + '\nport = {postMessage() {}}; pageScriptDeadline(new Promise(() => {}), Date.now() + 5);',
    { chrome, navigator: { userAgent: 'Edg/' }, importScripts: ignore, setTimeout, clearTimeout });
  await assert.rejects(expired, /web page did not respond/);
});

test('attached extension reads and edits an existing browser tab using Chrome debugger', async () => {
  const profile = await mkdtemp(path.join(os.tmpdir(), 'permissive-extension-'));
  const server = createServer((_request, response) => {
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    response.end('<html><body><input id="name"><button id="go" onclick="document.querySelector(\'p\').textContent=document.querySelector(\'input\').value">Save</button><p>signed-in fixture</p><div>' + 'Outlook toolbar '.repeat(200) + 'Full latest message café 日本語 🚀</div><div id="scroll-container" style="height:100px;overflow:auto"><div id="messages" role="listbox" style="height:100px"><div style="height:1000px">Newest message first</div></div></div></body></html>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const extension = path.resolve('browser/extension');
  const context = await chromium.launchPersistentContext(profile, { executablePath: chromium.executablePath(), headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`] });
  try {
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    assert.match(worker.url(), /mnldpjghemdjnoefihnifnpfhlaihamk/);
    const options = await context.newPage();
    await options.goto(worker.url().replace('background.js', 'options.html'));
    await options.getByLabel('Profile name').fill('Work profile');
    await options.getByRole('button', { name: 'Save', exact: true }).click();
    await options.getByRole('status').filter({ hasText: 'Profile name saved.' }).waitFor();
    const metadata = await worker.evaluate(async () => {
      const messages = [];
      port = { postMessage(value) { messages.push(value); } };
      await announceProfile();
      const first = messages.at(-1);
      await announceProfile();
      port = undefined;
      return { first, second: messages.at(-1) };
    });
    assert.equal(metadata.first.profileName, 'Work profile');
    assert.equal(metadata.first.browser, 'chrome');
    assert.equal(metadata.first.profileId, metadata.second.profileId);
    const localProfileId = 'local-' + 'a'.repeat(24);
    await options.goto(worker.url().replace('background.js', 'options.html') + '#' + new URLSearchParams({ localProfileId, profileName: 'Paul · work@example.com' }));
    await options.getByRole('button', { name: 'Save and connect', exact: true }).waitFor();
    assert.equal(await options.getByLabel('Profile name').inputValue(), 'Paul · work@example.com');
    // Exercise pairing without requiring a native host registration in this isolated fixture.
    await worker.evaluate(() => {
      globalThis.pairingMessages = [];
      port = { postMessage(message) { globalThis.pairingMessages.push(message); } };
    });
    await options.getByRole('button', { name: 'Save and connect', exact: true }).click();
    await options.getByRole('status').filter({ hasText: 'Profile connected.' }).waitFor();
    const bound = await worker.evaluate(async () => {
      const value = globalThis.pairingMessages.find(message => message.operation === 'hello' && message.localProfileId);
      port = undefined; enabled = false; clearTimeout(retry);
      return value;
    });
    assert.equal(bound.localProfileId, localProfileId);
    assert.equal(bound.profileId, metadata.first.profileId);
    await options.close();
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const result = await worker.evaluate(url => operate({ operation: 'list', url }), page.url());
    const tabId = result.tabs.find(tab => tab.url === page.url()).tabId;
    const operation = args => worker.evaluate(args => operate(args), { ...args, tabId });
    const backgrounded = await context.newPage();
    await backgrounded.goto('data:text/html,<p>Another active tab</p>');
    assert.match(JSON.stringify(await operation({ operation: 'inspect' })), /signed-in fixture/);
    assert.equal(await page.evaluate(() => document.visibilityState), 'visible');
    assert.equal(await page.evaluate(() => document.hasFocus()), true);
    const smallTab = await worker.evaluate(async () => {
      const tab = await chrome.tabs.create({url:'data:text/html,<div id="short" role="listbox">One message</div>',active:false});
      await ready(tab.id);
      return tab.id;
    });
    assert.deepEqual(await worker.evaluate(tabId => operate({operation:'scroll',tabId,selector:'#short',direction:'top'}), smallTab), {scrollTop:0,scrollable:false});
    await assert.rejects(worker.evaluate(tabId => operate({operation:'scroll',tabId,selector:'#missing',direction:'top'}), smallTab), /Element was not found; inspect/);
    await worker.evaluate(tabId => chrome.tabs.remove(tabId), smallTab);
    await backgrounded.close();
    const snapshot = await operation({ operation: 'inspect' });
    assert.match(snapshot.text, /Full latest message café 日本語 🚀/);
    assert.equal(snapshot.nextOffset, null);
    const save = snapshot.elements.find(element => element.name === 'Save');
    assert.ok(save.selector);
    const scoped = await operation({ operation: 'inspect', selector: '#messages' });
    assert.equal(scoped.text, 'Newest message first');
    await operation({ operation: 'scroll', selector: '#messages', direction: 'bottom' });
    assert.ok(await page.evaluate(() => document.querySelector('#scroll-container').scrollTop > 0));
    assert.ok((await operation({ operation: 'scroll', selector: '#messages', direction: 'top' })).scrollTop === 0);
    await operation({ operation: 'wait', selector: '#messages' });
    await page.evaluate(() => {
      const list = document.createElement('div');
      list.id = 'nested-list'; list.setAttribute('role', 'listbox'); list.style.height = '100px';
      list.innerHTML = '<div id="nested-scrollport" style="height:100px;overflow:auto"><div style="height:1000px">Nested virtual list</div></div>';
      document.body.append(list);
    });
    assert.ok((await operation({operation:'scroll',selector:'#nested-list',direction:'bottom'})).scrollTop > 0);
    assert.equal(await page.evaluate(() => document.querySelector('#nested-scrollport').scrollTop), 900);
    assert.equal((await operation({operation:'scroll',selector:'#nested-list',direction:'top'})).scrollTop, 0);
    await operation({ operation: 'type', selector: '#name', text: 'attached edit' });
    await operation({ operation: 'click', selector: save.selector });
    assert.match(JSON.stringify(await operation({ operation: 'inspect' })), /attached edit/);
    // The AA-style web-component hierarchy must work with inspected refs in every action.
    await page.evaluate(() => {
      for (let index = 0; index < 300; index++) {
        const decoration = document.createElement('site-decoration');
        decoration.attachShadow({ mode: 'open' }).innerHTML = '<span>Decoration</span>';
        document.body.append(decoration);
      }
      const host = document.createElement('aa-login'); document.body.append(host);
      host.innerHTML = '<span slot="button-label">Continue login</span>';
      host.attachShadow({ mode: 'open' }).innerHTML = '<label>Account<input id="shadow-user"></label><button id="shadow-save"><slot name="button-label"></slot></button><p id="shadow-result"></p>';
      host.shadowRoot.querySelector('button').onclick = () => host.shadowRoot.querySelector('p').textContent = 'Selected ' + host.shadowRoot.querySelector('input').value;
    });
    const component = await operation({ operation: 'inspect' });
    assert.equal(component.domTraversalTruncated, false);
    const account = component.elements.find(item => item.name === 'Account');
    const continueLogin = component.elements.find(item => item.name === 'Continue login');
    assert.ok(account.ref); assert.ok(continueLogin.ref);
    await operation({ operation: 'wait', selector: account.ref });
    await operation({ operation: 'type', selector: account.ref, text: 'fixture-account' });
    await operation({ operation: 'click', selector: continueLogin.ref });
    assert.match((await operation({ operation: 'inspect' })).text, /Selected fixture-account/);
    assert.equal(await operation({ operation: 'evaluate', script: '() => 42' }), 42);
    assert.equal(await operation({ operation: 'evaluate', script: 'return 43;' }), 43);
    await page.evaluate(() => document.querySelector('aa-login').remove());
    await assert.rejects(operation({ operation: 'click', selector: account.ref }), /reference is stale; inspect/);
    await page.evaluate(() => {
      const host = document.createElement('header-login'); host.style.cssText = 'display:inline-block;padding:10px'; document.body.append(host);
      host.attachShadow({ mode: 'open' }).innerHTML = '<a style="pointer-events:none" href="#fixture-login">Retargeted login</a>';
      host.onclick = () => host.dataset.clicked = 'yes';
    });
    const retarget = (await operation({ operation: 'inspect' })).elements.find(item => item.name === 'Retargeted login');
    await operation({ operation: 'click', selector: retarget.ref });
    assert.equal(await page.evaluate(() => document.querySelector('header-login').dataset.clicked), 'yes');
    await page.evaluate(() => {
      const cover = document.createElement('div'); cover.id = 'fixture-cover';
      cover.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:white'; document.body.append(cover);
    });
    await assert.rejects(operation({ operation: 'click', selector: retarget.ref }), /covered by another control/);
    await page.evaluate(() => { document.querySelector('#fixture-cover').remove(); document.querySelector('header-login').remove(); });
    await page.evaluate(() => {
      for (let n = 0; n < 150; n++) document.body.prepend(Object.assign(document.createElement('button'), { textContent: 'Toolbar action ' + n }));
      document.querySelector('#messages > div').setAttribute('role', 'option');
    });
    const crowded = await operation({ operation: 'inspect' });
    assert.ok(crowded.elements.some(element => element.role === 'listbox'));
    assert.ok(crowded.elements.some(element => element.role === 'option' && element.name === 'Newest message first'));
    assert.ok(crowded.totalElements > 100);
    assert.equal(crowded.nextElementOffset, 100);
    const moreControls = await operation({ operation: 'inspect', elementOffset: crowded.nextElementOffset });
    assert.ok(moreControls.elements.length > 0);
    assert.ok(!crowded.elements.some(first => first.ref === moreControls.elements[0].ref));
    assert.ok((await operation({ operation: 'screenshot' })).length > 100);
    await operation({ operation: 'release' });
    const releasedBackground = await context.newPage();
    await releasedBackground.goto('data:text/html,<p>Normal browser focus after release</p>');
    assert.equal(await page.evaluate(() => document.hasFocus()), false);
    await releasedBackground.close();
    const opened = await worker.evaluate(url => operate({ operation: 'open', url }), page.url());
    assert.equal(opened.url, page.url());
    assert.ok(Number.isInteger(opened.tabId));
    const navigated = await worker.evaluate(args => operate(args), { operation: 'open', tabId: opened.tabId, url: page.url() + '/second' });
    assert.equal(navigated.tabId, opened.tabId);
    await worker.evaluate(tabId => chrome.tabs.remove(tabId), opened.tabId);
    // Exercise the real scripting API when debugger attachment is denied by an
    // extension-owned frame. No access to that other extension is attempted.
    await context.route('https://www.fixture.test/**', route => route.fulfill({ contentType: 'text/html', body:
      '<credential-form></credential-form><script>const host=document.querySelector("credential-form");host.attachShadow({mode:"open"}).innerHTML=\'<form action="https://login.fixture.test/session"><label>Account<input id="account"></label><label>Password<input type="password" id="password"></label><button type="button">Continue</button><p></p></form>\';host.shadowRoot.querySelector("button").onclick=()=>host.shadowRoot.querySelector("p").textContent="Fixture continued";</script>' })) ;
    const fallbackPage = await context.newPage();
    await fallbackPage.goto('https://www.fixture.test/');
    const fallbackTab = (await worker.evaluate(() => operate({ operation: 'list', url: 'https://www.fixture.test/' }))).tabs[0].tabId;
    await worker.evaluate(tabId => {
      globalThis.originalAttach = chrome.debugger.attach;
      chrome.debugger.attach = async (...args) => {
        if (args[0].tabId === tabId) throw new Error('Cannot access a chrome-extension:// URL of different extension');
        return globalThis.originalAttach(...args);
      };
    }, fallbackTab);
    const fallback = args => worker.evaluate(args => operate(args), { ...args, tabId: fallbackTab });
    const fallbackSnapshot = await fallback({ operation: 'inspect' });
    assert.equal(fallbackSnapshot.browserControl.transport, 'page-script');
    const fallbackUser = fallbackSnapshot.elements.find(item => item.name === 'Account');
    const fallbackPassword = fallbackSnapshot.elements.find(item => item.name === 'Password');
    const fallbackButton = fallbackSnapshot.elements.find(item => item.name === 'Continue');
    await fallback({ operation: 'wait', selector: fallbackUser.ref });
    await fallback({ operation: 'type', selector: fallbackUser.ref, text: 'synthetic-account' });
    assert.deepEqual(await fallback({ operation: 'credentialContext', selector: fallbackPassword.ref, field: 'password' }),
      { origin: 'https://www.fixture.test', formOrigins: ['https://www.fixture.test', 'https://login.fixture.test'] });
    const fill = { origin: 'https://www.fixture.test', formOrigins: ['https://www.fixture.test', 'https://login.fixture.test'],
      selector: fallbackPassword.ref, field: 'password', value: 'synthetic-password', expiresAt: Math.floor(Date.now() / 1000) + 15 };
    assert.deepEqual(await fallback({ operation: 'credentialFill', payload: fill }), { ok: true });
    assert.equal(await fallbackPage.locator('#password').inputValue(), 'synthetic-password');
    await assert.rejects(fallback({ operation: 'credentialFill', payload: { ...fill, origin: 'https://login.fixture.test' } }), /Secure fill failed/);
    const expiredClick = await worker.evaluate(({tabId, selector}) => chrome.scripting.executeScript({target:{tabId,frameIds:[0]}, args:[selector],
      func: async selector => {
        try { return await globalThis.permissiveAction({ operation:'click', selector, actionDeadline:0 }); }
        catch (error) { return { error:error.message }; }
      } }), {tabId:fallbackTab, selector:fallbackButton.ref});
    assert.match(expiredClick[0].result.error, /Page action expired/);
    assert.doesNotMatch((await fallback({ operation: 'inspect' })).text, /Fixture continued/);
    await fallbackPage.evaluate(() => document.querySelector('credential-form').shadowRoot.querySelector('form').action = 'https://outside.invalid/');
    await assert.rejects(fallback({ operation: 'credentialFill', payload: fill }), /Secure fill failed/);
    assert.equal((await fallback({ operation: 'click', selector: fallbackButton.ref })).interaction, 'dom');
    assert.match((await fallback({ operation: 'inspect' })).text, /Fixture continued/);
    await fallbackPage.evaluate(() => {
      const cover = document.createElement('div'); cover.style.cssText = 'position:fixed;inset:0;z-index:2147483647'; document.body.append(cover);
    });
    await assert.rejects(fallback({ operation: 'click', selector: fallbackButton.ref }), /covered by another control/);
    await assert.rejects(fallback({ operation: 'evaluate', script: '42' }), /Use inspect and structured page actions/);
    await fallback({ operation: 'release' });
    await worker.evaluate(() => {
      chrome.debugger.attach = globalThis.originalAttach;
      globalThis.originalSendCommand = chrome.debugger.sendCommand;
      chrome.debugger.sendCommand = async () => { throw new Error('Cannot access a chrome-extension:// URL of different extension'); };
    });
    assert.equal((await fallback({ operation: 'inspect' })).browserControl.transport, 'page-script');
    await fallback({ operation: 'release' });
    // A policy restriction must never trigger the scripting fallback.
    await worker.evaluate(() => { chrome.debugger.attach = async () => { throw new Error('Host access is restricted by policy.'); }; });
    await assert.rejects(fallback({ operation: 'inspect' }), /Host access is restricted by policy/);
    await worker.evaluate(() => {
      chrome.debugger.attach = globalThis.originalAttach; chrome.debugger.sendCommand = globalThis.originalSendCommand;
      delete globalThis.originalAttach; delete globalThis.originalSendCommand;
    });
    await fallbackPage.close();
  } finally {
    await context.close();
    await new Promise(resolve => server.close(resolve));
    await rm(profile, { recursive: true, force: true });
  }
});
