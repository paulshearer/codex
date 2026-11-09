import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdirSync, readFileSync } from 'node:fs';
import { build } from 'esbuild';
import { chromium } from 'playwright';

test('Windows pairing QR stays local and supports enrollment, revocation and disable', async () => {
  const bundled = await build({ stdin: { contents: `
    import React from 'react'; import { createRoot } from 'react-dom/client';
    import { MobilePairing } from './desktop/mobile-pairing.jsx';
    const state = {enabled:false, devices:[]}; window.fixtureCalls = [];
    const deviceId = 'a'.repeat(64);
    const api = {
      mobileStatus: async () => ({...state, devices:state.devices.map(value => ({...value}))}),
      mobilePair: async () => { window.fixtureCalls.push('pair'); if(window.failNextPair) {window.failNextPair=false;state.enabled=false;throw Error('Pairing setup failed');} state.enabled = true; state.origin = 'https://windows.example.ts.net:9444';
        state.devices = [{deviceId, deviceName:'My Android', revoked:false}];
        return {version:1, origin:state.origin, installationId:'fixture-installation', invitationId:'fixture', secret:'LOCAL-QR-SECRET', expiresAt:Math.floor(Date.now()/1000)+300}; },
      mobileRevoke: async id => { window.fixtureCalls.push(id); state.devices[0].revoked=true; },
      mobileDisable: async () => {window.fixtureCalls.push('disable'); state.enabled=false;},
    };
    const root=createRoot(document.getElementById('root'));
    root.render(<MobilePairing api={api} onClose={() => root.unmount()} />);
  `, resolveDir: process.cwd(), loader: 'jsx' }, bundle: true, write: false, platform: 'browser', format: 'iife' });
  const source = bundled.outputFiles[0].text;
  const css = readFileSync(new URL('../desktop/style.css', import.meta.url), 'utf8');
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', request.url === '/app.js' ? 'application/javascript' : 'text/html');
    response.end(request.url === '/app.js' ? source : `<style>${css}</style><div id="root"></div><script src="/app.js"></script>`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => requests.push(request.url()));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByText('Companion access is disabled.').waitFor();
    await page.getByRole('button', { name: 'Create pairing QR', exact: true }).click();
    await page.getByLabel('Android pairing QR').waitFor();
    assert.ok(await page.getByLabel('Android pairing QR').evaluate(canvas => canvas.toDataURL().length > 1000));
    await page.getByText('Pairing code for manual entry').click();
    assert.equal(JSON.parse(await page.getByLabel('Pairing code', {exact:true}).inputValue()).secret, 'LOCAL-QR-SECRET');
    mkdirSync('out/mobile', { recursive:true });
    await page.getByText('Pairing code for manual entry').click();
    await page.locator('.mobile-pairing').evaluate(panel => { panel.scrollTop = 0; });
    await page.screenshot({path:'out/mobile/windows-pairing-qr.png'});
    await page.evaluate(() => {window.failNextPair=true;});
    await page.getByRole('button', { name:'Create another pairing QR', exact:true }).click();
    await page.getByRole('alert').waitFor();
    assert.equal(await page.locator('canvas').count(), 0);
    await page.getByRole('button', { name:'Create pairing QR', exact:true }).click();
    await page.getByLabel('Android pairing QR').waitFor();
    await page.getByRole('button', { name: 'Revoke', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.mobile-device button')?.disabled);
    await page.getByRole('button', { name: 'Disable companion access', exact: true }).click();
    await page.getByText('Companion access is disabled.').waitFor();
    assert.equal(await page.locator('canvas').count(), 0);
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.ok(requests.every(url => url.startsWith(`http://127.0.0.1:${server.address().port}`)));
    assert.deepEqual(errors, []);
    mkdirSync('out/mobile', { recursive:true });
    await page.screenshot({path:'out/mobile/windows-pairing.png'});
    await page.getByRole('button', {name:'Close', exact:true}).click();
    assert.equal(await page.getByRole('dialog').count(), 0);
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});
