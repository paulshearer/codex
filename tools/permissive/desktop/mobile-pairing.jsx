import React, { useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';

export function MobilePairing({ api = window.permissive, onClose }) {
  const [state, setState] = useState(null), [invitation, setInvitation] = useState(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [now, setNow] = useState(Date.now());
  const canvas = useRef(null), alive = useRef(true);
  async function refresh() {
    const result = await api.mobileStatus();
    if (alive.current) { setState(result); if (!result.enabled) setInvitation(null); }
  }
  useEffect(() => {
    alive.current = true;
    refresh().catch(() => { if (alive.current) setError('Cannot read companion status.'); });
    const timer = setInterval(() => { setNow(Date.now()); refresh().catch(() => {}); }, 3000);
    return () => { alive.current = false; clearInterval(timer); };
  }, []);
  const remaining = invitation ? Math.max(0, invitation.expiresAt - Math.floor(now / 1000)) : 0;
  useEffect(() => {
    if (invitation && remaining > 0 && canvas.current) {
      QRCode.toCanvas(canvas.current, JSON.stringify(invitation), { width: 300, margin: 2, errorCorrectionLevel: 'M' })
        .catch(() => { if (alive.current) setError('Cannot draw the pairing QR. Use the pairing code below.'); });
    }
  }, [invitation]);
  async function run(operation) {
    setBusy(true); setError('');
    try { await operation(); }
    catch (failure) { if (alive.current) setError(failure.message || 'Companion operation failed.'); }
    finally { await refresh().catch(() => {}); if (alive.current) setBusy(false); }
  }
  return <div className="table-modal-backdrop"><div className="table-modal-panel mobile-pairing" role="dialog" aria-modal="true" aria-label="Pair Android">
    <header><h2>Pair Android</h2><button disabled={busy} onClick={onClose}>Close</button></header>
    <p>Open Permissive Codex on your phone, connect Tailscale, then scan this Windows installation’s pairing QR.</p>
    <p className="muted">Each enrolled phone authenticates with its own protected key. Chat history stays on Windows.</p>
    {error && <p role="alert" className="error">{error}</p>}
    {state && <p role="status">{state.enabled ? `Companion enabled · ${state.origin}` : 'Companion access is disabled.'}</p>}
    <div className="mobile-pairing-actions"><button className="primary" disabled={busy || !state} onClick={() => run(async () => {
      setInvitation(null);
      const result = await api.mobilePair();
      if (alive.current) { setInvitation(result); setNow(Date.now()); }
    })}>{busy ? 'Working…' : invitation ? 'Create another pairing QR' : 'Create pairing QR'}</button>
    {state?.enabled && <button disabled={busy} onClick={() => run(async () => { await api.mobileDisable(); setInvitation(null); })}>Disable companion access</button>}</div>
    {invitation && remaining > 0 && <div className="mobile-pairing-code"><canvas ref={canvas} aria-label="Android pairing QR" />
      <p>One-use invitation · expires in {Math.ceil(remaining / 60)} minute{remaining > 60 ? 's' : ''}.</p>
      <details><summary>Pairing code for manual entry</summary><textarea aria-label="Pairing code" readOnly value={JSON.stringify(invitation)} /></details>
    </div>}
    {invitation && remaining === 0 && <p role="status">This invitation has expired. Create another QR to pair.</p>}
    <h3>Enrolled phones</h3>
    {!state?.devices?.length && <p className="muted">No phones enrolled yet.</p>}
    {(state?.devices || []).map(device => <div className="mobile-device" key={device.deviceId || device.id}>
      <div><strong>{device.deviceName || device.name || 'Android phone'}</strong><small>{(device.deviceId || device.id || '').slice(0, 16)}{device.revoked ? ' · Revoked' : ''}</small></div>
      <button disabled={busy || device.revoked} onClick={() => run(() => api.mobileRevoke(device.deviceId || device.id))}>Revoke</button>
    </div>)}
  </div></div>;
}
