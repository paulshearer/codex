import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

// Console arguments can contain prompts, page content and credentials.
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir', 'dirxml', 'table', 'assert', 'count', 'countReset', 'time', 'timeEnd', 'timeLog', 'group', 'groupCollapsed', 'groupEnd']) {
  console[method] = () => {};
}
const readyListeners = new Set(), errorListeners = new Set(), eventListeners = new Set();
const watchConnectedListeners = new Set();
let ready = false;
function failure(value) { return value instanceof Error ? value : new Error(typeof value === 'string' ? value : 'Desktop operation failed'); }
async function call(operation, ...args) {
  try { return await invoke('desktop_call', { operation, args: args.map(value => value ?? null) }); }
  catch (error) { throw failure(error); }
}
const subscribed = Promise.all([listen('task-event', event => {
  for (const listener of eventListeners) listener(event.payload);
}), listen('task-stream-connected', event => {
  for (const listener of watchConnectedListeners) listener(event.payload);
})]);
function register(listeners, callback) { listeners.add(callback); return () => listeners.delete(callback); }
const api = {};
const operations = { saveTable: 'save-table', list: 'list', submit: 'submit', get: 'get', cancel: 'cancel', continue: 'continue', pause: 'pause', resume: 'resume', retry: 'retry', taskMenu: 'task-menu',
  credentials: 'credentials', prepareCredential: 'prepare-credential', saveCredential: 'save-credential', revokeCredential: 'revoke-credential',
  mobileStatus: 'mobile-status', mobilePair: 'mobile-pair', mobileRevoke: 'mobile-revoke', mobileDisable: 'mobile-disable',
  deleteChat: 'delete-chat', deleteWorkspace: 'delete-workspace', renameChat: 'rename-chat', forkChat: 'fork-chat', deleteFrom: 'delete-from', editMessage: 'edit-message', deleteMessage: 'delete-message', regenerate: 'regenerate',
  attachFiles: 'attach-files', uploadAttachment: 'upload-attachment', attachmentInfo: 'attachment-info', saveAttachment: 'save-attachment', removeAttachment: 'remove-attachment',
  chats: 'chats', chat: 'chat', chatEvents: 'chat-events', steer: 'steer', queueFollowup: 'queue-followup', removeFollowup: 'remove-followup', resumeQueue: 'resume-queue',
  workspaces: 'workspaces', saveWorkspace: 'save-workspace', skills: 'skills', skillDocument: 'skill-document', skillCategory: 'skill-category', registerSkills: 'register-skills',
  importSkills: 'import-skills', exportSkills: 'export-skills', browsers: 'browsers', connectBrowserProfile: 'connect-browser-profile', setupBrowserProfile: 'setup-browser-profile',
  copyExtensionFolder: 'copy-extension-folder', chooseDirectory: 'choose-directory', chooseSkills: 'choose-skills', artifact: 'artifact' };
for (const [method, operation] of Object.entries(operations)) api[method] = (...args) => call(operation, ...args);
api.health = async () => {
  try {
    const result = await call('health');
    if (!ready) { ready = true; for (const listener of readyListeners) listener(); }
    return result;
  } catch (error) { for (const listener of errorListeners) listener(error.message); throw error; }
};
api.watch = async (id, after = 0, conversation = false) => {
  await subscribed;
  try { return await invoke('desktop_watch', { id, after, conversation }); } catch (error) { throw failure(error); }
};
api.unwatch = () => invoke('desktop_unwatch');
api.onEvent = callback => register(eventListeners, callback);
api.onWatchConnected = callback => register(watchConnectedListeners, callback);
api.onReady = callback => { const remove = register(readyListeners, callback); if (ready) queueMicrotask(callback); return remove; };
api.onError = callback => register(errorListeners, callback);
Object.defineProperty(window, 'permissive', { value: Object.freeze(api), writable: false, configurable: false });
