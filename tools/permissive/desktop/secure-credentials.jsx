import React, { useEffect, useRef, useState } from 'react';
import { sealCredential } from './credential-envelope.mjs';

export function SecureCredentials({ workspaceId, onClose }) {
  const [entries, setEntries] = useState([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [lifetime, setLifetime] = useState('session'), [hours, setHours] = useState(4);
  const label = useRef(), origin = useRef(), username = useRef(), password = useRef();
  const dialog = useRef();
  const live = useRef(true);
  const clear = () => { if (password.current) password.current.value = ''; if (username.current) username.current.value = ''; };
  async function refresh() { const result = await window.permissive.credentials(workspaceId); if (live.current) setEntries(result.data); }
  useEffect(() => {
    live.current = true;
    dialog.current?.showModal();
    refresh().catch(() => { if (live.current) setError('Secure credentials are unavailable. Check the execution service.'); });
    const timer = setInterval(() => { refresh().catch(() => {}); }, 5000);
    const hidden = () => { if (document.hidden) clear(); };
    document.addEventListener('visibilitychange', hidden);
    return () => { live.current = false; clearInterval(timer); clear(); document.removeEventListener('visibilitychange', hidden); };
  }, [workspaceId]);
  async function save() {
    if (busy) return;
    setError('');
    let credential;
    try {
      const parsed = new URL(origin.current.value);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash || !['', '/'].includes(parsed.pathname)) throw new Error();
      if (!label.current.value.trim() || !username.current.value || !password.current.value ||
          (lifetime === 'hours' && (!Number.isInteger(Number(hours)) || hours < 1 || hours > 168))) throw new Error();
      credential = { label: label.current.value.trim(), origin: parsed.origin, username: username.current.value,
        password: password.current.value, lifetime, ...(lifetime === 'hours' ? { hours: Number(hours) } : {}) };
    } catch { setError('Enter a label, HTTPS website origin, username, password, and a duration of 1–168 hours or Session.'); return; }
    setBusy(true);
    try {
      const prepared = await window.permissive.prepareCredential(workspaceId);
      const envelope = await sealCredential(prepared, workspaceId, credential);
      clear(); credential = null;
      await window.permissive.saveCredential(envelope);
      if (live.current) await refresh();
    } catch { if (live.current) setError('Handoff could not be confirmed. Refresh the list before entering credentials again.'); }
    finally { credential = null; if (live.current) setBusy(false); }
  }
  async function revoke(id) {
    setBusy(true); setError('');
    try { await window.permissive.revokeCredential(workspaceId, id); await refresh(); }
    catch { if (live.current) setError('Revocation could not be confirmed. Refresh and retry.'); }
    finally { if (live.current) setBusy(false); }
  }
  return <dialog ref={dialog} aria-label="Secure credentials" className="secure-credentials" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="panel-heading"><strong>Secure credentials</strong><button type="button" disabled={busy} onClick={onClose}>Close</button></div>
    <p>Available to Codex in this workspace. Each entry uses a fresh encryption key held only in the execution service’s memory.</p>
    <p className="muted">Includes sign-in subdomains of the same registered website over HTTPS at the same port, such as www.aa.com and login.aa.com. Other websites and hosted tenants stay separate.</p>
    <p className="muted">Session lasts until you revoke the entry or the execution service restarts. Timed entries also clear on restart. Expiry stops future fills; it does not sign out website sessions.</p>
    {entries.map(entry => <div key={entry.id} className="credential-entry"><span><strong>{entry.label}</strong><br />{entry.origin}<br /><small>{entry.expiresAt ? `Expires ${new Date(entry.expiresAt * 1000).toLocaleString()}` : 'Session'}</small></span><button type="button" disabled={busy} onClick={() => revoke(entry.id)}>Revoke</button></div>)}
    <fieldset disabled={busy}>
      <label>Label<input ref={label} aria-label="Credential label" maxLength={80} autoComplete="off" placeholder="Work account" /></label>
      <label>Website origin<input ref={origin} aria-label="Credential website origin" type="url" autoComplete="off" placeholder="https://example.com" maxLength={512} /></label>
      <label>Username<input ref={username} aria-label="Credential username" autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={2048} /></label>
      <label>Password<input ref={password} aria-label="Credential password" type="password" autoComplete="new-password" maxLength={8192} /></label>
      <label>Keep available<select aria-label="Credential lifetime" value={lifetime} onChange={event => setLifetime(event.target.value)}><option value="session">Session</option><option value="hours">Number of hours</option></select></label>
      {lifetime === 'hours' && <label>Hours<input aria-label="Credential hours" type="number" min="1" max="168" value={hours} onChange={event => setHours(event.target.value)} /></label>}
      <button type="button" className="primary" onClick={save}>Provide securely</button>
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
    <button type="button" disabled={busy} onClick={() => revoke(null)}>Revoke all in this workspace</button>
  </dialog>;
}
