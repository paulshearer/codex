import React, { useEffect, useRef, useState } from 'react';
import { conversationAttachments } from './conversation.mjs';

export function ConversationAttachments({ tasks, events }) {
  const files = conversationAttachments(tasks, events), dialog = useRef(null), trigger = useRef(null);
  const [open, setOpen] = useState(false), [error, setError] = useState('');
  useEffect(() => { if (open) dialog.current?.showModal(); }, [open]);
  function close() { setOpen(false); trigger.current?.focus(); }
  const inputs = files.filter(file => file.input), artifacts = files.filter(file => !file.input);
  return <div className="conversation-attachments">
    <button ref={trigger} type="button" className="attachments-button" onClick={() => setOpen(true)}>Attachments ({files.length})</button>
    {open && <dialog ref={dialog} className="table-modal-panel attachments-dialog" aria-label="Attachments" onCancel={close} onClick={event => { if (event.target === event.currentTarget) close(); }}>
      <header><h2>Attachments ({files.length})</h2><button autoFocus onClick={close}>Close attachments</button></header>
      <div className="attachments-scroll">
        {!!inputs.length && <><h3>Attached files</h3><AttachmentList attachments={inputs} /></>}
        {!!artifacts.length && <><h3>Generated files</h3><ul className="attachment-list">{artifacts.map(file => <li className="attachment-card" key={`${file.taskId}:${file.id}`}><div><span title={file.name || file.id}>{file.name || file.id}</span></div><button onClick={async () => {
          try { setError(''); await window.permissive.artifact(file.taskId, file.id); } catch (error) { setError(error.message); }
        }}>Save</button></li>)}</ul></>}
        {error && <p className="error" role="alert">{error}</p>}
      </div>
    </dialog>}
  </div>;
}

export function AttachmentList({ attachments = [], onRemove, disabled = false }) {
  const [details, setDetails] = useState({}), [error, setError] = useState('');
  const ids = attachments.map(item => typeof item === 'string' ? item : item.id);
  useEffect(() => {
    let live = true;
    for (const id of ids) window.permissive.attachmentInfo(id).then(value => {
      if (live) setDetails(previous => ({ ...previous, [id]: value }));
    }).catch(() => { if (live) setError('An attached file is unavailable.'); });
    return () => { live = false; };
  }, [JSON.stringify(ids)]);
  async function save(id) {
    try { setError(''); await window.permissive.saveAttachment(id); }
    catch (error) { setError(error.message); }
  }
  if (!ids.length) return null;
  return <><ul className="attachment-list" aria-label={onRemove ? 'Draft attachments' : 'Message attachments'}>
    {ids.map((id, index) => {
      const file = details[id] || (typeof attachments[index] === 'object' ? attachments[index] : { name: 'Attached file' });
      return <li key={id} className="attachment-card">
        {file.preview && <img src={file.preview} alt={`Preview of ${file.name}`} />}
        <div><span title={file.name}>{file.name}</span>{file.size != null && <small>{file.size < 1024 ? `${file.size} B` : file.size < 1024 * 1024 ? `${(file.size / 1024).toFixed(1)} KiB` : `${(file.size / 1024 / 1024).toFixed(1)} MiB`}</small>}</div>
        {onRemove ? <button type="button" disabled={disabled} aria-label={`Remove attachment ${file.name}`} onClick={() => onRemove(id)}>×</button>
          : <button type="button" onClick={() => save(id)} aria-label={`Save attachment ${file.name}`}>Save</button>}
      </li>;
    })}
  </ul>{error && <p className="error" role="alert">{error}</p>}</>;
}

export function readAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('Cannot read pasted image'));
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.readAsDataURL(file);
  });
}
