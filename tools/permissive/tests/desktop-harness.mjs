import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';

// Only the separate debug + e2e binary supports this loopback inspection port.
export async function launchDesktop({ root, home, apiUrl, python, sourcePath = root, dialogs = {}, executablePath }) {
  const binary = executablePath || process.env.PERMISSIVE_DESKTOP_TEST_BINARY || 'C:/codex-build/permissive-desktop/debug/CodexPermissive.exe';
  if (!existsSync(binary)) throw new Error('Build the Rust desktop with windows/build-desktop.ps1 -Debug -E2E before UI tests.');
  const listener = createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const dialogFile = path.join(home, 'ui-test-dialogs.json');
  writeFileSync(dialogFile, JSON.stringify(dialogs));
  const child = spawn(binary, [], { cwd: root, windowsHide: true, stdio: 'ignore', env: { ...process.env,
    PERMISSIVE_PACKAGE: root, PERMISSIVE_HOME: home, PERMISSIVE_PYTHON: python,
    PERMISSIVE_SOURCE_PATH: sourcePath, PERMISSIVE_API_URL: apiUrl,
    PERMISSIVE_E2E_DEBUG_PORT: String(port), PERMISSIVE_E2E_DIALOG_FILE: dialogFile } });
  let browser;
  try {
    for (let retry = 0; retry < 150; retry++) {
      if (child.exitCode !== null) throw new Error('Rust desktop exited before its window became ready.');
      try { const response = await fetch(`http://127.0.0.1:${port}/json/version`); if (response.ok) { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`); break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    if (!browser) throw new Error('Rust desktop inspection did not become ready.');
    let page;
    for (let retry = 0; retry < 100; retry++) {
      page = browser.contexts().flatMap(context => context.pages()).find(page => page.url().includes('tauri.localhost'));
      if (page) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!page) throw new Error('Rust desktop application page is unavailable.');
    await page.waitForLoadState('domcontentloaded');
    return {
      firstWindow: async () => page, process: () => child,
      configureDialogs: values => writeFileSync(dialogFile, JSON.stringify(values)),
      close: async () => {
        await browser.close();
        if (child.exitCode === null) { const ended = new Promise(resolve => child.once('exit', resolve)); child.kill(); await ended; }
        await new Promise(resolve => setTimeout(resolve, 300));
      },
    };
  } catch (error) { child.kill(); if (browser) await browser.close(); throw error; }
}
