import assert from 'node:assert/strict';
import path from 'node:path';
import { launchDesktop } from './desktop-harness.mjs';

// Read-only installed UI check. Do not record chat text, account names or page contents.
const root = path.join(process.env.LOCALAPPDATA, 'Programs/CodexPermissive/1.0.0-tauri');
const home = path.join(process.env.LOCALAPPDATA, 'CodexPermissive');
let app;
try {
  app = await launchDesktop({ root, home, python: path.join(root, 'python/python.exe'), sourcePath: path.join(root, 'lib'), apiUrl: 'http://127.0.0.1:8789' });
  const page = await app.firstWindow();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.locator('.connection').filter({ hasText: 'Service connected' }).waitFor();
  const ids = await page.evaluate(async () => {
    const tasks = (await window.permissive.list()).data;
    const target = tasks.find(task => task.status === 'completed' && task.tokenUsage);
    return { task: target.id, workspace: target.workspaceId };
  });
  const index = await page.evaluate(async id => (await window.permissive.workspaces()).data.findIndex(workspace => workspace.id === id), ids.workspace);
  await page.locator('.workspace').nth(index).click();
  await page.waitForFunction(async id => (await window.permissive.list((await window.permissive.get(id)).workspaceId)).data.length === document.querySelectorAll('.task').length, ids.task);
  const taskIndex = await page.evaluate(async id => (await window.permissive.list((await window.permissive.get(id)).workspaceId)).data.findIndex(task => task.id === id), ids.task);
  await page.locator('.task').nth(taskIndex).click();
  await page.locator('.chat-status-row [role=status]').filter({ hasText: 'Done' }).waitFor();
  const original = await page.locator('.chat-status-row [role=status]').innerText();
  await page.waitForTimeout(2200);
  assert.equal(await page.locator('.chat-status-row [role=status]').innerText(), original);
  await page.getByRole('button', { name: 'Stats', exact: true }).click();
  await page.locator('.token-usage pre').first().waitFor();
  const counts = JSON.parse(await page.locator('.token-usage pre').last().innerText());
  assert.deepEqual(Object.keys(counts), ['cachedInputTokens', 'inputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens']);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, installedRuntime: true, completedTimerFrozen: true, fiveUsageFieldsVisible: true, pageErrors: 0 }));
} finally {
  if (app) await app.close();
}
