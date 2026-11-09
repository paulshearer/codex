import path from 'node:path';

export function launchPreferences(env) {
  const browser = env.PERMISSIVE_BROWSER || 'chromium';
  const profile = env.PERMISSIVE_BROWSER_PROFILE || '';
  if (!['chromium', 'chrome', 'msedge'].includes(browser)) throw new Error('Unknown browser');
  if (profile && (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(profile) || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(profile))) throw new Error('Invalid browser profile');
  return {
    directory: profile ? path.join(env.PERMISSIVE_HOME, 'browser-profiles', browser, profile)
      : path.join(env.PERMISSIVE_HOME, 'browsers', ...(browser === 'chromium' ? [] : [browser]), env.PERMISSIVE_BROWSER_SESSION_ID || env.PERMISSIVE_TASK_ID),
    options: { headless: env.PERMISSIVE_HEADLESS === '1', acceptDownloads: true,
      ...(browser === 'chromium' ? {} : { channel: browser }) },
  };
}
