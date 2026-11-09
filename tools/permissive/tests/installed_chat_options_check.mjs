import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
import { launchDesktop } from './desktop-harness.mjs';

// Exercise the debug-only UI against a selected installed runtime. Never print
// account names, chat contents, page text, or exception bodies from a live home.
const root = process.env.PERMISSIVE_PACKAGE || path.join(process.env.LOCALAPPDATA, 'Programs/CodexPermissive/1.0.0-tauri');
const home = process.env.PERMISSIVE_HOME || path.join(process.env.LOCALAPPDATA, 'CodexPermissive');
mkdirSync(home, { recursive: true });
const report = { passed: false, menuVerified: false, browserOptionsVerified: false,
  planVerified: false, goalVerified: false, libraryImportExportVerified: false, pageErrors: 0 };
let application;
try {
  application = await launchDesktop({ root, home, python: path.join(root, 'python/python.exe'),
    sourcePath: path.join(root, 'lib'), apiUrl: process.env.PERMISSIVE_API_URL || 'http://127.0.0.1:8789', dialogs: { confirm: false } });
  const page = await application.firstWindow();
  page.on('pageerror', () => { report.pageErrors++; });
  await page.locator('.connection').filter({ hasText: 'Service connected' }).waitFor({ timeout: 30000 });
  await page.getByRole('button', { name: 'Chat options', exact: true }).click();
  for (const label of ['Browser and profile', 'Plan', 'Goal', 'Skills']) {
    assert.equal(await page.getByRole('menu').getByText(label, { exact: true }).count(), 1);
  }
  report.menuVerified = true;
  await page.getByRole('menuitem', { name: 'Browser and profile' }).click();
  await page.getByRole('combobox', { name: 'Browser', exact: true }).waitFor();
  assert.ok(await page.getByRole('combobox', { name: 'Browser', exact: true }).locator('option').count());
  await page.getByRole('combobox', { name: 'Browser profile', exact: true }).waitFor();
  report.browserOptionsVerified = true;
  await page.getByRole('button', { name: 'Close chat options' }).click();
  await page.getByRole('button', { name: 'Chat options', exact: true }).click();
  await page.getByRole('menuitemradio', { name: 'Plan', exact: true }).click();
  await page.getByRole('button', { name: 'Plan task', exact: true }).waitFor();
  report.planVerified = true;
  await page.getByRole('button', { name: 'Chat options', exact: true }).click();
  await page.getByRole('menuitemradio', { name: 'Goal', exact: true }).click();
  await page.getByRole('textbox', { name: 'Goal objective' }).waitFor();
  report.goalVerified = true;
  await page.getByRole('button', { name: 'Close chat options' }).click();
  await page.getByRole('button', { name: 'Skills', exact: true }).click();
  await page.getByRole('button', { name: 'Import skills…', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Export selected skills…', exact: true }).waitFor();
  report.libraryImportExportVerified = true;
  assert.equal(report.pageErrors, 0);
  report.passed = true;
} catch {
  throw new Error('Installed Rust desktop UI verification failed. No private page data was recorded.');
} finally {
  if (application) await application.close();
  if (process.env.PERMISSIVE_UI_CHECK_REPORT) writeFileSync(process.env.PERMISSIVE_UI_CHECK_REPORT, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
}
