importScripts('dom.js');
importScripts('inspection.js');
importScripts('credential-fill.js');
let port;
let retry;
let enabled = false;
const attached = new Set();
const pageScriptTabs = new Set();

async function announceProfile() {
  const stored = await chrome.storage.local.get(['profileId', 'profileName', 'localProfileId']);
  const profileId = stored.profileId || crypto.randomUUID();
  if (!stored.profileId) await chrome.storage.local.set({ profileId });
  port?.postMessage({ operation: 'hello', browser: navigator.userAgent.includes('Edg/') ? 'msedge' : 'chrome',
    profileId, profileName: stored.profileName || `Browser profile ${profileId.slice(0, 8)}`, localProfileId: stored.localProfileId || '' });
}

function connect() {
  enabled = true;
  chrome.storage.local.set({ connectEnabled: true });
  if (port) {
    announceProfile().catch(() => chrome.action.setBadgeText({ text: 'ERR' }));
    return;
  }
  clearTimeout(retry);
  port = chrome.runtime.connectNative('com.codex_permissive.browser');
  port.onDisconnect.addListener(() => { port = null; chrome.action.setBadgeText({ text: 'OFF' }); if (enabled) retry = setTimeout(connect, 3000); });
  port.onMessage.addListener(async (message) => {
    try {
      const result = await operate(message);
      port?.postMessage({ id: message.id, result });
    } catch (error) { port?.postMessage({ id: message.id, error: error.message }); }
  });
  chrome.action.setBadgeText({ text: 'ON' });
  announceProfile().catch(() => chrome.action.setBadgeText({ text: 'ERR' }));
}

function debuggerError(error) {
  // A web tab may contain a frame owned by another extension. Only this
  // debugger limitation permits the ordinary web-page scripting fallback.
  // Policy restrictions and user cancellation must continue to fail.
  if (error.message === 'Cannot access a chrome-extension:// URL of different extension') {
    const restricted = new Error('An extension-owned frame prevents debugger access. Use inspect and structured page actions; evaluate, upload and debugger screenshots are unavailable on this tab.');
    restricted.extensionFrameRestriction = true;
    return restricted;
  }
  return error;
}

async function cdp(tabId, method, params = {}) {
  if (!attached.has(tabId)) {
    try { await chrome.debugger.attach({ tabId }, '1.3'); }
    catch (error) { throw debuggerError(error); }
    attached.add(tabId);
  }
  try { return await chrome.debugger.sendCommand({ tabId }, method, params); }
  catch (error) { throw debuggerError(error); }
}

async function evaluate(tabId, expression) {
  // Apps such as Outlook suspend virtual list rendering in a hidden tab.
  await cdp(tabId, 'Page.bringToFront');
  // Windows occlusion can still mark a foreground tab hidden. Keep the
  // controlled page focused while this debugger attachment is active.
  await cdp(tabId, 'Emulation.setFocusEmulationEnabled', { enabled: true });
  const value = await cdp(tabId, 'Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (value.exceptionDetails) throw new Error(value.exceptionDetails.exception?.description || value.exceptionDetails.text);
  return value.result.value;
}

function pageScriptDeadline(promise, deadline) {
  let timer;
  return Promise.race([promise, new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error('The web page did not respond. Reload it once using an observed URL, then inspect again; report the blocker if it persists.')), Math.max(0, deadline - Date.now()));
  })]).finally(() => clearTimeout(timer));
}

async function pageScript(tabId, operation, args) {
  const actionDeadline = Date.now() + 25000;
  const tab = await chrome.tabs.get(tabId);
  if (!/^https?:\/\//.test(tab.url || '')) throw new Error('Page actions require an ordinary HTTP or HTTPS website');
  const window = await chrome.windows.get(tab.windowId);
  await chrome.windows.update(tab.windowId, { focused: true, ...(window.state === 'minimized' ? { state: 'normal' } : {}) });
  await chrome.tabs.update(tabId, { active: true });
  // Fixed, packaged code runs in this extension's isolated world, in the main
  // frame only. Never inject into a different extension or evaluate strings.
  const target = { tabId, frameIds: [0] };
  await pageScriptDeadline(chrome.scripting.executeScript({ target, world: 'ISOLATED', files: ['dom.js', 'inspection.js', 'credential-fill.js'] }), actionDeadline);
  const results = await pageScriptDeadline(chrome.scripting.executeScript({ target, world: 'ISOLATED', args: [{ ...args, operation, actionDeadline }], func: async function (args) {
    try {
      if (Date.now() >= args.actionDeadline) throw new Error('Page action expired; inspect again');
      const dom = globalThis.permissiveDOM();
      const result = args.operation === 'inspect' ? await globalThis.permissiveInspect(args, dom) :
        args.operation === 'credentialFill' ? await globalThis.permissiveCredentialFill(args, dom) : await globalThis.permissiveAction(args, dom);
      return { ok: true, result };
    } catch (error) { return { ok: false, error: args.operation === 'credentialFill' ? 'Secure fill failed' : error.message }; }
  } }), actionDeadline);
  const answer = results.find(result => result.frameId === 0)?.result;
  if (!answer?.ok) throw new Error(answer?.error || 'Page changed during the action; inspect again');
  return answer.result;
}

async function pageDOM(tabId, operation, args = {}) {
  if (!pageScriptTabs.has(tabId)) {
    const action = operation === 'inspect' ? permissiveInspect : operation === 'credentialFill' ? permissiveCredentialFill : permissiveAction;
    try { return await evaluate(tabId, permissiveExpression(action, { ...args, operation })); }
    catch (error) {
      if (!error.extensionFrameRestriction) throw error;
      pageScriptTabs.add(tabId);
    }
  }
  const result = await pageScript(tabId, operation, args);
  return operation === 'inspect' ? { ...result, browserControl: { transport: 'page-script', trustedPointerEvents: false,
    hint: 'Use returned refs with structured actions. Inspect after clicking; evaluate, upload and debugger screenshots are unavailable on this tab.' } } : result;
}

async function ready(tabId) {
  for (let attempt = 0; attempt < 300; attempt++) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === 'complete') return { tabId, url: tab.url };
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const tab = await chrome.tabs.get(tabId);
  return { tabId, url: tab.url, loading: true, hint: 'Inspect the page to check its current state' };
}

async function operate(message) {
  const { tabId, operation, selector, text, script, url, file, offset = 0, limit, direction, elementOffset, elementLimit } = message;
  if (operation === 'list') {
    const tabs = (await chrome.tabs.query({})).filter(tab => !url || tab.url?.includes(url));
    const count = Math.max(1, Math.min(50, limit || 20));
    return { tabs: tabs.slice(offset, offset + count).map(tab => ({ tabId: tab.id, url: tab.url?.slice(0, 240), title: tab.title?.slice(0, 120) })),
      nextOffset: offset + count < tabs.length ? offset + count : null, totalTabs: tabs.length };
  }
  if (operation === 'release') {
    if (attached.has(tabId)) {
      try { await cdp(tabId, 'Emulation.setFocusEmulationEnabled', { enabled: false }); } catch {}
      try { await chrome.debugger.detach({ tabId }); } catch {}
    }
    attached.delete(tabId); pageScriptTabs.delete(tabId); return { ok: true };
  }
  if (operation === 'open') {
    if (tabId === undefined) {
      const tab = await chrome.tabs.create({ url, active: false });
      return ready(tab.id);
    }
    await chrome.tabs.update(tabId, { url });
    return ready(tabId);
  }
  if (operation === 'wait') {
    await ready(tabId);
    if (selector) return pageDOM(tabId, 'wait', { selector });
    return { ready: true };
  }
  if (operation === 'screenshot') return (await cdp(tabId, 'Page.captureScreenshot', { format: 'png' })).data;
  if (operation === 'evaluate') return evaluate(tabId, permissiveScript(script));
  if (operation === 'credentialFill') {
    try {
      await pageDOM(tabId, operation, message.payload);
      return { ok: true };
    } catch { throw new Error('Secure fill failed'); }
  }
  if (operation === 'inspect') return pageDOM(tabId, operation, { selector, offset, limit, elementOffset, elementLimit });
  if (operation === 'credentialContext') return pageDOM(tabId, operation, { selector, field: message.field });
  if (operation === 'scroll') return pageDOM(tabId, operation, { selector, direction });
  if (operation === 'upload') {
    const handle = await cdp(tabId, 'Runtime.evaluate', { expression: permissiveExpression(function (args, dom) { return dom.one(args.selector); }, { selector }), returnByValue: false });
    if (handle.exceptionDetails || !handle.result.objectId) throw new Error('Upload input was not found; inspect again');
    try {
      const { nodeId } = await cdp(tabId, 'DOM.requestNode', { objectId: handle.result.objectId });
      return await cdp(tabId, 'DOM.setFileInputFiles', { nodeId, files: [file] });
    } finally { await cdp(tabId, 'Runtime.releaseObject', { objectId: handle.result.objectId }); }
  }
  if (operation === 'download') {
    const link = await pageDOM(tabId, 'link', { selector });
    if (!link) throw new Error('Download selector must select a link');
    const downloadId = await chrome.downloads.download({ url: link, saveAs: false });
    for (let attempt = 0; attempt < 120; attempt++) {
      const [download] = await chrome.downloads.search({ id: downloadId });
      if (download.state === 'complete') return { downloadId, file: download.filename };
      if (download.state === 'interrupted') throw new Error('Download interrupted');
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error('Download did not complete within one minute');
  }
  if (operation === 'click') {
    const point = await pageDOM(tabId, 'clickPoint', { selector });
    if (pageScriptTabs.has(tabId)) return pageScript(tabId, 'click', { selector });
    await cdp(tabId, 'Input.dispatchMouseEvent', { type: 'mouseMoved', ...point });
    await cdp(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
    await cdp(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
    await new Promise(resolve => setTimeout(resolve, 250));
    return { ok: true };
  }
  if (operation === 'type') return pageDOM(tabId, operation, { selector, text });
  throw new Error('Unsupported operation');
}

chrome.debugger.onDetach.addListener(source => attached.delete(source.tabId));
chrome.tabs.onRemoved.addListener(tabId => { attached.delete(tabId); pageScriptTabs.delete(tabId); });
chrome.action.onClicked.addListener(connect);
chrome.storage.onChanged.addListener((_changes, area) => { if (area === 'local' && port) announceProfile(); });
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.operation === 'connect' && sender.id === chrome.runtime.id) { connect(); reply({ ok: true }); }
});
chrome.storage.local.get('connectEnabled').then(value => { if (value.connectEnabled) connect(); });
