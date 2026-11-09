import React, { useEffect, useRef, useState } from 'react';

export function RenameChat({ initialTitle, onSave, onClose }) {
  const dialog = useRef(null), submitting = useRef(false);
  const [title, setTitle] = useState(initialTitle), [saving, setSaving] = useState(false), [error, setError] = useState('');
  const trimmed = title.trim();
  const valid = trimmed.length > 0 && [...trimmed].length <= 120 && !/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(trimmed);
  useEffect(() => { dialog.current.showModal(); }, []);
  async function save(event) {
    event.preventDefault();
    if (!valid || submitting.current) return;
    submitting.current = true; setSaving(true); setError('');
    try { await onSave(trimmed); }
    catch (error) { setError(error.message); }
    finally { submitting.current = false; setSaving(false); }
  }
  return <dialog ref={dialog} className="rename-dialog" aria-labelledby="rename-chat-heading" onCancel={event => { event.preventDefault(); if (!submitting.current) onClose(); }}>
    <form onSubmit={save}>
      <h2 id="rename-chat-heading">Rename chat</h2>
      <label>Chat name<input autoFocus aria-label="Chat name" value={title} disabled={saving} onChange={event => { setTitle(event.target.value); setError(''); }} /></label>
      <p className="muted">Use a single line of up to 120 characters.</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="table-modal-actions"><button type="button" disabled={saving} onClick={onClose}>Cancel</button><button className="primary" disabled={!valid || saving}>{saving ? 'Saving…' : 'Save'}</button></div>
    </form>
  </dialog>;
}
