import React, { useEffect, useRef, useState } from 'react';
import { AttachmentList, readAttachment } from './attachments.jsx';
import { BrowserSettings } from './browser-settings.jsx';
import { SecureCredentials } from './secure-credentials.jsx';
import { SkillList, SkillPreview } from './skills.jsx';
import { terminalStatuses } from './conversation.mjs';

const fields = ['browserMode', 'browser', 'browserProfile', 'skills', 'mode', 'goal'];
const browserNames = { chromium: 'Chromium', chrome: 'Chrome', msedge: 'Edge' };
function capturedSettings(task) {
  return Object.fromEntries(fields.filter(field => task?.request[field] !== undefined).map(field => [field, task.request[field]]));
}

export function ChatComposer({ workspace, task, prompt, setPrompt, disabled, onSubmit }) {
  const storageKey = task ? `permissive-chat-options:${task.conversationId || task.threadId || task.id}` : null;
  const [settings, setSettings] = useState(() => {
    try { return { ...capturedSettings(task), ...(storageKey ? JSON.parse(localStorage.getItem(storageKey) || localStorage.getItem(`permissive-chat-options:${task.threadId || task.id}`) || '{}') : {}) }; }
    catch { return capturedSettings(task); }
  });
  const [menu, setMenu] = useState(false), [panel, setPanel] = useState(null), [catalog, setCatalog] = useState([]), [preview, setPreview] = useState(null), [error, setError] = useState('');
  const [attachments, setAttachments] = useState([]), [attaching, setAttaching] = useState(false);
  const attachmentLock = useRef(false);
  const attachmentBytes = attachments.reduce((sum, file) => sum + file.size, 0);
  async function attach(files = null) {
    if (disabled || attachmentLock.current) return;
    attachmentLock.current = true; setAttaching(true); setError(''); setMenu(false);
    const uploaded = [];
    try {
      if (files) {
        if (attachments.length + files.length > 10) throw new Error('Attach at most 10 files per message.');
        if (files.some(file => !file.size || file.size > 16 * 1024 * 1024)) throw new Error('Files must be nonempty and at most 16 MiB.');
        if (attachmentBytes + files.reduce((sum, file) => sum + file.size, 0) > 32 * 1024 * 1024) throw new Error('Attachments exceed the 32 MiB message limit.');
        for (const file of files) uploaded.push(await window.permissive.uploadAttachment({ workspaceId: workspace.id, name: file.name || `pasted-image-${Date.now()}.png`, data: await readAttachment(file) }));
      } else uploaded.push(...await window.permissive.attachFiles(workspace.id, 10 - attachments.length, 32 * 1024 * 1024 - attachmentBytes));
      setAttachments(previous => [...previous, ...uploaded]);
    } catch (error) {
      for (const file of uploaded) await window.permissive.removeAttachment(file.id).catch(() => {});
      setError(error.message);
    } finally { attachmentLock.current = false; setAttaching(false); }
  }
  function paste(event) {
    const files = Array.from(event.clipboardData?.items || []).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter(Boolean);
    if (files.length) {
      event.preventDefault();
      if (attachmentLock.current) { setError('Wait for the current attachments to finish before pasting another image.'); return; }
      const text = event.clipboardData.getData('text/plain');
      if (text) { const input = event.currentTarget; setPrompt(prompt.slice(0, input.selectionStart) + text + prompt.slice(input.selectionEnd)); }
      void attach(files);
    }
  }
  async function removeAttachment(id) {
    setAttachments(previous => previous.filter(file => file.id !== id));
    // A delivery with uncertain status may already own this file. Retain its
    // managed copy in the chat while removing it from the unsent draft.
    await window.permissive.removeAttachment(id).catch(() => {});
  }
  const effective = { browserMode: 'dedicated', browser: 'chromium', browserProfile: '', skills: [], mode: 'default', goal: '', ...workspace, ...settings };
  const browserSelection = `${effective.browserMode}:${effective.browser}:${effective.browserProfile}`;
  const [browserStatus, setBrowserStatus] = useState(null);
  useEffect(() => {
    if (effective.browserMode !== 'attached' && !effective.browserProfile?.startsWith('local-')) return;
    let live = true, pending = false;
    async function refresh() {
      if (!live || pending) return;
      pending = true;
      try {
        const catalog = await window.permissive.browsers();
        const profile = catalog.data.find(item => item.id === effective.browser)?.installedProfiles?.find(item => item.id === effective.browserProfile);
        const connections = (catalog.connections || []).filter(item => effective.browser === 'chromium' || item.browser === effective.browser);
        const matches = connections.filter(item => !effective.browserProfile || item.profileId === effective.browserProfile || item.localProfileId === effective.browserProfile);
        if (live) setBrowserStatus({ selection: browserSelection, label: profile?.account || profile?.name || (effective.browserProfile && matches[0]?.profileName) || '', connected: matches.length === 1, ambiguous: matches.length > 1 });
      } catch {
        if (live) setBrowserStatus(previous => ({ selection: browserSelection, label: previous?.selection === browserSelection ? previous.label : '', connected: null }));
      } finally { pending = false; }
    }
    function refreshVisible() { if (document.visibilityState !== 'hidden') void refresh(); }
    void refresh();
    const timer = effective.browserMode === 'attached' ? setInterval(refreshVisible, 5000) : null;
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      live = false; clearInterval(timer);
      window.removeEventListener('focus', refreshVisible);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [browserSelection, panel]);
  const selectedBrowserStatus = browserStatus?.selection === browserSelection ? browserStatus : null;
  const profileLabel = selectedBrowserStatus?.label || effective.browserProfile || (effective.browserMode === 'attached' ? 'Any profile' : 'Conversation profile');
  const connectionLabel = !selectedBrowserStatus ? 'Checking…' : selectedBrowserStatus.connected === null ? 'Status unavailable' : selectedBrowserStatus.ambiguous ? 'Choose a profile' : selectedBrowserStatus.connected ? 'Connected' : 'Not connected';
  function change(values) {
    setSettings(previous => {
      const next = { ...previous, ...values };
      if (storageKey) localStorage.setItem(storageKey, JSON.stringify(next));
      return next;
    });
  }
  useEffect(() => {
    if (panel !== 'skills') return;
    let live = true; setError('');
    window.permissive.skills(workspace?.id, task?.request.workingDirectory || workspace?.workingDirectory).then(result => { if (live) setCatalog(result.data); }).catch(error => { if (live) setError(error.message); });
    return () => { live = false; };
  }, [panel, workspace?.id, task?.request.workingDirectory]);
  useEffect(() => {
    if (!menu) return;
    function dismiss(event) { if (!event.target.closest('.chat-add')) setMenu(false); }
    function escape(event) { if (event.key === 'Escape') setMenu(false); }
    document.addEventListener('pointerdown', dismiss); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape); };
  }, [menu]);
  function open(name) { setPanel(name); setMenu(false); }
  function mode(name) { change({ mode: name, goal: name === 'goal' ? effective.goal : '' }); open(name === 'goal' ? 'goal' : null); }
  async function addSkillFiles() {
    try { const paths = await window.permissive.chooseSkills(); if (paths.length) { await window.permissive.registerSkills(paths); change({ skills: [...new Set([...effective.skills, ...paths])] }); setCatalog((await window.permissive.skills(workspace?.id, workspace?.workingDirectory)).data); } }
    catch (error) { setError(error.message); }
  }
  const active = task && !terminalStatuses.has(task.status);
  async function submit(event, action = active ? 'steer' : 'continue') {
    event.preventDefault();
    if (panel === 'credentials') return;
    if (disabled || attaching || (!prompt.trim() && !attachments.length)) return;
    if (action === 'steer' && (task.status !== 'running' || !task.activeTurnId)) return;
    if (await onSubmit(settings, action, attachments.map(file => file.id))) { setMenu(false); setPanel(null); setAttachments([]); }
  }
  return <form className="chat-composer" onSubmit={submit}>
    {panel === 'credentials' && <SecureCredentials key={workspace.id} workspaceId={workspace.id} onClose={() => setPanel(null)} />}
    <div className="workspace-context"><span title={task?.request.workingDirectory || workspace?.workingDirectory}>{task?.request.workingDirectory || workspace?.workingDirectory}</span><small>{workspace?.systemPrompt ? 'Workspace prompt' : 'Default prompt'}</small></div>
    {panel && panel !== 'credentials' && <div className="chat-settings-panel" role="region" aria-label="Chat options">
      <div className="settings-section-head"><h2>{panel === 'browser' ? 'Browser for this chat' : panel === 'skills' ? 'Skills for this chat' : 'Goal for this chat'}</h2><button type="button" onClick={() => setPanel(null)}>Close chat options</button></div>
      {panel === 'browser' && <><BrowserSettings draft={effective} change={change} /><button type="button" onClick={() => change({ browserMode: workspace.browserMode, browser: workspace.browser || 'chromium', browserProfile: workspace.browserProfile || '' })}>Use workspace browser</button></>}
      {panel === 'skills' && <><p className="muted">Choose the specific skills to use in this chat. These selections replace its workspace skill defaults.</p>
        <SkillList catalog={[...catalog, ...effective.skills.filter(path => !catalog.some(skill => skill.path === path)).map(path => ({ path, name: path.split(/[\\/]/).at(-2), description: 'Assigned skill' }))]} selected={effective.skills} onSelect={paths => change({ skills: paths })} onPreview={setPreview} maxSelected={32} />
        <div className="skill-toolbar"><button type="button" onClick={addSkillFiles}>Add skill files…</button><button type="button" onClick={() => change({ skills: workspace.skills })}>Use workspace skills</button><button type="button" onClick={() => change({ skills: [] })}>Clear chat skills</button></div>
        {preview && <SkillPreview path={preview} onClose={() => setPreview(null)} />}</>}
      {panel === 'goal' && <><label>Goal objective<textarea aria-label="Goal objective" value={effective.goal} placeholder="Leave empty to use your chat message as the objective" onChange={event => change({ goal: event.target.value })} /></label><p className="muted">The agent will continue toward this goal across turns until it completes or stops. Pause and Cancel remain available.</p></>}
      {error && <p className="error" role="alert">{error}</p>}
      <p className="muted">Selections apply to the next message in this chat.</p>
    </div>}
    <div className="chat-selection-summary">
      <button type="button" onClick={() => open('browser')} aria-label="Change chat browser">{browserNames[effective.browser] || effective.browser} · {profileLabel}{effective.browserMode === 'attached' ? ` · ${connectionLabel}` : ''}</button>
      <button type="button" onClick={() => open('skills')} aria-label="Change chat skills">{effective.skills.length} skill{effective.skills.length === 1 ? '' : 's'}</button>
      {effective.mode !== 'default' && <span className="mode-chip">{effective.mode === 'plan' ? 'Plan · read only' : 'Goal'}<button type="button" aria-label={`Remove ${effective.mode} mode`} onClick={() => mode('default')}>×</button></span>}
    </div>
    <AttachmentList attachments={attachments} onRemove={removeAttachment} disabled={disabled || attaching} />
    {attaching && <p className="muted" role="status">Adding attachments…</p>}
    {!panel && error && <p className="error" role="alert">{error}</p>}
    <div className="chat-input-row"><div className="chat-add"><button className="chat-add-button" type="button" aria-label="Chat options" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu(previous => !previous)}>＋</button>
      {menu && <div className="chat-add-menu" role="menu" aria-label="Chat additions">
        <button type="button" role="menuitem" disabled={disabled || attaching || attachments.length >= 10} onClick={() => attach()}>Attach files…</button>
        <button type="button" role="menuitem" onClick={() => open('browser')}>Browser and profile</button>
        <button type="button" role="menuitem" onClick={() => { setMenu(false); setPanel('credentials'); }}>Secure credentials</button>
        <button type="button" role="menuitemradio" aria-checked={effective.mode === 'plan'} onClick={() => mode('plan')}>Plan</button>
        <button type="button" role="menuitemradio" aria-checked={effective.mode === 'goal'} onClick={() => mode('goal')}>Goal</button>
        <button type="button" role="menuitem" onClick={() => open('skills')}>Skills</button>
        {effective.mode !== 'default' && <button type="button" role="menuitemradio" aria-checked={false} onClick={() => mode('default')}>Default mode</button>}
      </div>}
    </div><textarea aria-label="Task prompt" placeholder={effective.mode === 'plan' ? 'Describe what you want to plan…' : 'Message or paste an image…'} value={prompt} onChange={event => setPrompt(event.target.value)} onPaste={paste} /></div>
    {effective.skills.length > 32 && <p className="error" role="alert">Select at most 32 skills for this chat.</p>}
    <div className="composer-actions">{active ? <><button className="primary" type="submit" disabled={disabled || attaching || (!prompt.trim() && !attachments.length) || task.status !== 'running' || !task.activeTurnId}>Steer now</button><button type="button" disabled={disabled || attaching || (!prompt.trim() && !attachments.length) || effective.skills.length > 32} onClick={event => submit(event, 'queue')}>Queue follow-up</button></>
      : <button className="primary" disabled={disabled || attaching || (!prompt.trim() && !attachments.length) || effective.skills.length > 32}>{task ? 'Continue conversation' : effective.mode === 'plan' ? 'Plan task' : effective.mode === 'goal' ? 'Start goal' : 'Run task'}</button>}</div>
  </form>;
}
