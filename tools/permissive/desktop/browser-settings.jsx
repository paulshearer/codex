import React, { useEffect, useState } from 'react';

export function BrowserSettings({ draft, change }) {
  const [catalog, setCatalog] = useState({ data: [], connections: [] }), [error, setError] = useState(''), [custom, setCustom] = useState(false);
  const [setup, setSetup] = useState(null), [connecting, setConnecting] = useState(false);
  async function refresh() { try { setCatalog(await window.permissive.browsers()); setError(''); } catch (error) { setError(error.message); } }
  useEffect(() => { refresh(); }, []);
  const attached = draft.browserMode === 'attached', browser = draft.browser || 'chromium', profile = draft.browserProfile || '';
  const connections = catalog.connections.filter(item => browser === 'chromium' || item.browser === browser);
  const profiles = catalog.data.find(item => item.id === browser)?.profiles || [];
  const existing = catalog.data.find(item => item.id === browser)?.installedProfiles || [];
  const selectedExisting = existing.find(item => item.id === profile);
  const extraConnections = connections.filter(item => item.profileId && (item.profileId === profile || !existing.some(profile => profile.id === item.localProfileId)));
  useEffect(() => { setSetup(null); setError(''); }, [browser, profile, attached]);
  async function connectProfile() {
    setConnecting(true); setError('');
    try { const result = await window.permissive.setupBrowserProfile(browser, profile); if (!result.cancelled) setSetup(result); await refresh(); }
    catch (error) { setError(error.message); }
    finally { setConnecting(false); }
  }
  return <div className="browser-settings">
    <label>Browser mode<select aria-label="Browser mode" value={draft.browserMode} onChange={event => {
      change({ browserMode: event.target.value, browser: event.target.value === 'attached' && browser === 'chromium' ? 'chrome' : browser, browserProfile: '' }); setCustom(false);
    }}><option value="dedicated">Dedicated browser</option><option value="attached">Existing signed-in browser</option></select></label>
    <label>Browser<select aria-label="Browser" value={browser} onChange={event => { change({ browser: event.target.value, browserProfile: '' }); setCustom(false); }}>
      {attached && browser === 'chromium' && <option value="chromium">Any connected browser (legacy setting)</option>}
      {!attached && <option value="chromium">Bundled Chromium</option>}
      {['chrome', 'msedge'].map(id => { const item = catalog.data.find(value => value.id === id); return <option key={id} value={id} disabled={!attached && item && !item.installed}>{id === 'chrome' ? 'Google Chrome' : 'Microsoft Edge'}{!attached && item && !item.installed ? ' (not installed)' : ''}</option>; })}
    </select></label>
    <label>Browser profile<select aria-label="Browser profile" value={custom && !attached ? '__new' : profile} onChange={event => {
      setCustom(event.target.value === '__new'); change({ browserProfile: event.target.value === '__new' ? '' : event.target.value });
    }}>
      <option value="">{attached ? 'Any connected profile of this browser' : 'Separate profile for each conversation'}</option>
      {attached ? <><optgroup label="Profiles on this PC">{existing.map(item => <option key={item.id} value={item.id}>{item.name}{item.account ? ` · ${item.account}` : ''}{item.connected ? ' (connected)' : ' (not connected)'}</option>)}</optgroup>
        {!!extraConnections.length && <optgroup label="Other connected profiles">{extraConnections.map(item => <option key={item.connectionId} value={item.profileId}>{item.profileName}</option>)}</optgroup>}</> : profiles.map(name => <option key={name}>{name}</option>)}
      {profile && !(attached ? existing.some(item => item.id === profile) || connections.some(item => item.profileId === profile) : profiles.includes(profile)) && <option value={profile}>{profile}{attached ? ' (disconnected)' : ''}</option>}
      {!attached && <option value="__new">New named profile…</option>}
    </select></label>
    {custom && !attached && <label>New profile name<input aria-label="New profile name" required maxLength={64} pattern={'[A-Za-z0-9][A-Za-z0-9_\\-]{0,63}'} title="Use letters, numbers, underscores or hyphens" value={profile} onChange={event => change({ browserProfile: event.target.value })} placeholder="work" /></label>}
    <button type="button" onClick={refresh}>Refresh browser profiles</button>
    {attached && selectedExisting && <div className="existing-profile-setup">
      <p className="muted">{selectedExisting.connected ? 'This profile is connected and ready to use.' : 'This profile was found on your PC. Connect it once to let this chat use its signed-in tabs.'}</p>
      <button type="button" disabled={connecting} onClick={connectProfile}>{connecting ? 'Enabling and connecting…' : selectedExisting.connected ? 'Reconnect this profile' : 'Enable and connect'}</button>
      {connecting && <p role="status">Complete the confirmation, then keep the browser setup window visible while the app finishes.</p>}
      {setup?.connected && <p role="status">Browser profile connected. Ready to use in this chat.</p>}
    </div>}
    <p className="muted">{attached ? 'Enable and connect asks for confirmation, then enables Developer mode, loads the extension and connects the selected profile automatically. Connected profiles remain selected across reconnects.' : 'Dedicated profiles keep their own sign-ins and cookies. Named profiles can be reused across conversations; one task can use a profile at a time. To use an existing Chrome or Edge sign-in, choose Existing signed-in browser.'}</p>
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
