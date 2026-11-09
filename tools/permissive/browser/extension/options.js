let localProfileId = '', pairing = false;
async function showProfile() {
  const requested = new URLSearchParams(location.hash.slice(1));
  localProfileId = requested.get('localProfileId') || '';
  pairing = /^local-[a-f0-9]{24}$/.test(localProfileId);
  const value = await chrome.storage.local.get('profileName');
  document.querySelector('#name').value = (pairing ? requested.get('profileName') : '') || value.profileName || '';
  if (pairing) {
    document.querySelector('#binding').textContent = 'Connect this browser profile to the profile selected in Codex Permissive.';
    document.querySelector('button').textContent = 'Save and connect';
  }
}
showProfile();
window.addEventListener('hashchange', showProfile);
document.querySelector('form').addEventListener('submit', async event => {
  event.preventDefault();
  await chrome.storage.local.set({ profileName: document.querySelector('#name').value.trim(), ...(pairing ? { localProfileId } : {}) });
  if (pairing) await chrome.runtime.sendMessage({ operation: 'connect' });
  document.querySelector('#status').textContent = pairing ? 'Profile connected. Refresh browser profiles in Codex Permissive.' : 'Profile name saved.';
});
