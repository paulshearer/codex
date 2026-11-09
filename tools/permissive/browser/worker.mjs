import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
import { mkdirSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import './extension/dom.js';
import './extension/inspection.js';
import './extension/credential-fill.js';
import signing from '../client/signing.cjs';
import path from 'node:path';
import readline from 'node:readline';
import { launchPreferences } from './preferences.mjs';

const home = process.env.PERMISSIVE_HOME;
const taskId = process.env.PERMISSIVE_TASK_ID;
const attached = process.env.PERMISSIVE_BROWSER_MODE === 'attached';
const artifacts = path.join(home, 'artifacts', taskId);
const pages = new Map();
let context;
let sign;
const operations = ['credentials', 'fillCredential', 'list', 'open', 'inspect', 'click', 'type', 'evaluate', 'scroll', 'wait', 'screenshot', 'upload', 'download', 'release', 'artifact'];
const schema = { type: 'object', properties: {
  operation: { enum: operations }, pageId: { type: 'string' }, tabId: { type: 'integer' }, connectionId: { type: 'string' },
  url: { type: 'string' }, selector: { type: 'string' }, ref: { type: 'string', description: 'Element ref from the latest inspect snapshot' }, text: { type: 'string' }, script: { type: 'string' },
  credentialId: { type: 'string' }, field: { enum: ['username', 'password'] },
  file: { type: 'string' }, offset: { type: 'integer', minimum: 0 },
  limit: { type: 'integer', minimum: 1, maximum: 16000 }, direction: { enum: ['up', 'down', 'top', 'bottom'] },
  elementOffset: { type: 'integer', minimum: 0 }, elementLimit: { type: 'integer', minimum: 1, maximum: 200 },
}, required: ['operation'], additionalProperties: false };

function bounded(value) {
  let text = typeof value === 'string' ? value : JSON.stringify(value);
  if (Buffer.byteLength(text) > 65536) {
    mkdirSync(artifacts, { recursive: true });
    const artifactId = `browser-result-${randomUUID()}.json`;
    writeFileSync(path.join(artifacts, artifactId), JSON.stringify(value), 'utf8');
    text = JSON.stringify({ truncated: true, artifactId, path: path.join(artifacts, artifactId),
      preview: text.slice(0, 8000), description: 'Complete browser result saved as an artifact; inspect a narrower selector or paginate text with offset and limit.' });
  }
  return { content: [{ type: 'text', text }] };
}

async function browser() {
  if (!context) {
    await mkdir(artifacts, { recursive: true });
    const preferences = launchPreferences(process.env);
    try { context = await chromium.launchPersistentContext(preferences.directory, preferences.options); }
    catch (error) { throw new Error(`Cannot launch ${process.env.PERMISSIVE_BROWSER || 'chromium'} profile. Install the selected browser and close other tasks using this profile. ${error.message}`); }
    for (const page of context.pages()) pages.set(String(pages.size + 1), page);
  }
  return context;
}

async function attachedCall(args) {
  if (!sign) sign = signing.createSigner({ python: process.env.PERMISSIVE_PYTHON, home, scope: 'browser' });
  const selectedBrowser = process.env.PERMISSIVE_BROWSER;
  const body = JSON.stringify({ ...args, taskId,
    ...(selectedBrowser && selectedBrowser !== 'chromium' ? { browser: selectedBrowser } : {}),
    ...(process.env.PERMISSIVE_BROWSER_PROFILE ? { profileId: process.env.PERMISSIVE_BROWSER_PROFILE } : {}),
  });
  const response = await fetch('http://127.0.0.1:8789/v1/browser/command', {
    method: 'POST', headers: sign('POST', '/v1/browser/command', body, { 'Content-Type': 'application/json' }),
    body, signal: AbortSignal.timeout(125000), redirect: 'error',
  });
  if (!response.ok) throw new Error(await response.text());
  const value = await response.json();
  if (value.error) throw new Error(value.error);
  if (args.operation === 'screenshot') {
    const { writeFile } = await import('node:fs/promises');
    await mkdir(artifacts, { recursive: true });
    const file = `screenshot-${Date.now()}.png`;
    await writeFile(path.join(artifacts, file), Buffer.from(value.result, 'base64'));
    return bounded({ artifactId: file, path: path.join(artifacts, file) });
  }
  if (args.operation === 'download' && value.result.file) {
    const { copyFile } = await import('node:fs/promises');
    await mkdir(artifacts, { recursive: true });
    const file = `${Date.now()}-${path.basename(value.result.file).replace(/[^a-zA-Z0-9._-]/g, '_')}`;
    await copyFile(value.result.file, path.join(artifacts, file));
    return bounded({ artifactId: file, path: path.join(artifacts, file) });
  }
  return bounded(value);
}

async function operate(args) {
  if (!args || !operations.includes(args.operation)) throw new Error('Specify a browser operation. Start with list, then inspect the selected tab/page.');
  if (args.ref) args = { ...args, selector: args.ref };
  if (args.selector !== undefined && typeof args.selector !== 'string') throw new Error('Use a selector or ref returned by inspect');
  if (args.operation === 'credentials' || args.operation === 'fillCredential') return credentialOperation(args);
  if (args.operation === 'artifact') {
    const { copyFile } = await import('node:fs/promises');
    await mkdir(artifacts, { recursive: true });
    const file = `${Date.now()}-${path.basename(args.file).replace(/[^a-zA-Z0-9._-]/g, '_')}`;
    await copyFile(args.file, path.join(artifacts, file));
    return bounded({ artifactId: file, path: path.join(artifacts, file) });
  }
  if (attached) return attachedCall(args);
  await browser();
  if (args.operation === 'list') {
    const current = [...pages].filter(([, page]) => !page.isClosed() && (!args.url || page.url().includes(args.url)));
    const offset = args.offset || 0;
    const count = Math.max(1, Math.min(50, args.limit || 20));
    return bounded({ pages: current.slice(offset, offset + count).map(([pageId, page]) => ({ pageId, url: page.url().slice(0, 240) })),
      nextOffset: offset + count < current.length ? offset + count : null, totalPages: current.length });
  }
  if (args.operation === 'open') {
    const page = await context.newPage();
    const pageId = String(Date.now()) + '-' + pages.size;
    pages.set(pageId, page);
    await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    return bounded({ pageId, url: page.url() });
  }
  const page = pages.get(args.pageId);
  if (!page || page.isClosed()) throw new Error('Unknown pageId; call list first');
  await page.bringToFront();
  const locator = args.selector ? page.locator(args.selector) : null;
  switch (args.operation) {
    case 'inspect': {
      return bounded({ pageId: args.pageId, ...await page.evaluate(permissiveExpression(permissiveInspect, args)) });
    }
    case 'wait': if (locator) await page.evaluate(permissiveExpression(permissiveAction, { ...args, operation: 'wait' })); else await page.waitForLoadState('domcontentloaded'); break;
    case 'scroll': return bounded(await page.evaluate(permissiveExpression(permissiveAction, args)));
    case 'click': { const point = await page.evaluate(permissiveExpression(permissiveAction, { ...args, operation: 'clickPoint' })); await page.mouse.click(point.x, point.y); break; }
    case 'type': await page.evaluate(permissiveExpression(permissiveAction, args)); break;
    case 'evaluate': return bounded(await page.evaluate(permissiveScript(args.script)));
    case 'screenshot': {
      const file = `screenshot-${Date.now()}.png`;
      await page.screenshot({ path: path.join(artifacts, file), fullPage: true });
      return bounded({ artifactId: file, path: path.join(artifacts, file) });
    }
    case 'upload': {
      const handle = await page.evaluateHandle(permissiveExpression(function (args, dom) { return dom.one(args.selector); }, args));
      try { await handle.asElement().setInputFiles(args.file); } finally { await handle.dispose(); }
      break;
    }
    case 'download': {
      const promise = page.waitForEvent('download', { timeout: 60000 });
      const handle = await page.evaluateHandle(permissiveExpression(function (args, dom) { return dom.one(args.selector); }, args));
      try { await handle.asElement().click(); } finally { await handle.dispose(); }
      const download = await promise;
      const file = `${Date.now()}-${path.basename(download.suggestedFilename()).replace(/[^a-zA-Z0-9._-]/g, '_')}`;
      await download.saveAs(path.join(artifacts, file));
      return bounded({ artifactId: file, path: path.join(artifacts, file) });
    }
    case 'release': await page.close(); pages.delete(args.pageId); break;
    default: throw new Error('Unsupported operation');
  }
  return bounded({ ok: true });
}

async function credentialOperation(args) {
  // This transport is private to the worker; its response never goes to MCP.
  async function request(body) {
    if (!sign) sign = signing.createSigner({ python: process.env.PERMISSIVE_PYTHON, home, scope: 'browser' });
    const route = '/v1/browser/credentials', raw = JSON.stringify({ ...body, taskId });
    const response = await fetch('http://127.0.0.1:8789' + route, { method: 'POST',
      headers: sign('POST', route, raw, { 'Content-Type': 'application/json' }), body: raw,
      redirect: 'error', signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error('Credential unavailable. Check session expiry and the approved HTTPS website and port.');
    return response.json();
  }
  if (args.operation === 'credentials') return bounded(await request({ operation: 'list' }));
  if (!['username', 'password'].includes(args.field) || typeof args.selector !== 'string' || !args.selector) {
    throw new Error('fillCredential requires credentialId, field (username or password), and a sign-in input selector');
  }
  let stage = 'locate';
  try {
    let page, current;
    if (attached) {
      const result = await attachedCall({ operation: 'credentialContext', tabId: args.tabId, connectionId: args.connectionId, selector: args.selector, field: args.field });
      current = JSON.parse(result.content[0].text).result;
    } else {
      await browser();
      page = pages.get(args.pageId);
      if (!page || page.isClosed()) throw new Error();
      current = await page.evaluate(permissiveExpression(permissiveAction, { operation: 'credentialContext', selector: args.selector, field: args.field }));
    }
    stage = 'redeem';
    const secret = await request({ operation: 'fill', credentialId: args.credentialId, url: current.origin, formOrigins: current.formOrigins });
    const payload = { origin: secret.origin, selector: args.selector, field: args.field,
      value: secret[args.field], expiresAt: secret.expiresAt, formOrigins: secret.formOrigins };
    stage = 'apply';
    if (attached) {
      await attachedCall({ operation: 'credentialFill', tabId: args.tabId, connectionId: args.connectionId, payload });
    } else await page.evaluate(permissiveExpression(permissiveCredentialFill, payload));
    return bounded({ ok: true });
  } catch {
    // Playwright/CDP exceptions can include function arguments and field values.
    const reason = stage === 'locate' ? 'Inspect again and select a current visible sign-in input ref.' :
      stage === 'redeem' ? 'Credential expired, was revoked, or the page/form is outside its approved HTTPS website and port. Check Secure credentials.' :
      'The page, input, form destination or fill lease changed. Inspect again before retrying.';
    throw new Error('Secure fill failed. ' + reason);
  }
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let message;
  try {
    message = JSON.parse(line);
    if (message.id === undefined) continue;
    let result;
    switch (message.method) {
      case 'initialize': result = { protocolVersion: message.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'permissive-browser', version: '1.0.0' } }; break;
      case 'ping': result = {}; break;
      case 'tools/list': result = { tools: [{ name: 'browser', description: 'Control the selected browser profile. credentials lists temporary workspace credential IDs, labels, origins and expiry without values. fillCredential takes credentialId, field (username or password), an inspected input selector and pageId/tabId; it securely fills only the approved HTTPS website (including related sign-in subdomains at the same port) and returns success without values. Never read filled secrets back or save passwords. list returns up to 20 tabs/pages, filter by url substring. open with url creates a new tab/page; in attached mode tabId optionally navigates an existing tab. inspect returns up to 8000 text characters plus actionable elements with selectors; optionally inspect one selector, paginate text with offset/limit and controls with elementOffset/elementLimit using nextElementOffset. Use list then inspect, act using a returned ref or selector, and inspect again after navigation. Open shadow DOM and same-origin frames are included. Stale refs require a new inspect. Never guess selectors. Dismiss visible overlays before clicking covered controls. click/type/fillCredential accept ref. Passwords require fillCredential. Avoid evaluate for routine navigation. scroll with a selector finds its scrollable ancestor; use direction top for the beginning of a virtual message list. wait optionally waits for a selector. evaluate returns full structured DOM results; oversized results are saved as JSON artifacts. Screenshots/downloads return artifact IDs and local paths. Use artifact to expose a generated local file.', inputSchema: schema }] }; break;
      case 'tools/call':
        if (message.params.name !== 'browser') throw new Error('Unknown tool');
        try { result = await operate(message.params.arguments); }
        catch (error) { result = { ...bounded(error.message), isError: true }; }
        break;
      default: throw new Error('Unknown method');
    }
    console.log(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }));
  } catch (error) {
    console.log(JSON.stringify({ jsonrpc: '2.0', id: message?.id ?? null, error: { code: -32000, message: error.message } }));
  }
}
await context?.close();
