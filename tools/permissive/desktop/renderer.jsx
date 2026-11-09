import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { statusLabel, terminalStatuses as terminal, elapsedSeconds, taskTelemetry } from './conversation.mjs';
import { SkillList, SkillPreview, SkillLibrary } from './skills.jsx';
import { BrowserSettings } from './browser-settings.jsx';
import { ChatComposer } from './chat-composer.jsx';
import { Transcript, ConversationDetails } from './transcript.jsx';
import { Splitter } from './splitter.jsx';
import { MobilePairing } from './mobile-pairing.jsx';
import { ConversationAttachments } from './attachments.jsx';
import { RenameChat } from './rename-chat.jsx';

const api = window.permissive;
function WorkspaceEditor({ initial, onSave, onClose, guestPaths }) {
  const [draft, setDraft] = useState(initial || { name: '', workingDirectory: guestPaths ? 'C:\\workspaces' : 'C:\\git', systemPrompt: '', skills: [], browserMode: 'dedicated' });
  const [catalog, setCatalog] = useState([]), [preview, setPreview] = useState(null), [error, setError] = useState(''), [saving, setSaving] = useState(false);
  function change(key, value) { setDraft(previous => ({ ...previous, [key]: value })); setError(''); }
  useEffect(() => {
    let live = true;
    api.skills(initial?.id, draft.workingDirectory).then(value => { if (live) setCatalog(value.data); }).catch(error => { if (live) setError(error.message); });
    return () => { live = false; };
  }, [draft.workingDirectory, initial?.id]);
  async function save(event) {
    event.preventDefault();
    if (draft.skills.length > 32) { setError(`Select up to 32 skills. Currently ${draft.skills.length} are selected.`); return; }
    setSaving(true); setError('');
    try {
      const { name, workingDirectory, systemPrompt, skills, browserMode } = draft;
      await onSave(initial?.id, { name, workingDirectory, systemPrompt, skills, browserMode, browser: draft.browser || 'chromium', browserProfile: draft.browserProfile || '', executionDetail: draft.executionDetail || 'standard', diagnosticLogLevel: draft.diagnosticLogLevel || 'off' });
    } catch (error) { setError(error.message); }
    finally { setSaving(false); }
  }
  async function browseDirectory() { try { const path = await api.chooseDirectory(); if (path) change('workingDirectory', path); } catch (error) { setError(error.message); } }
  async function importSkills() {
    try {
      const paths = await api.chooseSkills();
      const selected = [...new Set([...draft.skills, ...paths])];
      if (selected.length > 32) { setError(`These files would select ${selected.length} skills. Choose up to 32; your current selection has been kept.`); return; }
      change('skills', selected);
    } catch (error) { setError(error.message); }
  }
  const available = [...catalog, ...draft.skills.filter(path => !catalog.some(skill => skill.path === path)).map(path => ({ path, name: path.split(/[\\/]/).at(-2) || path, description: 'Selected skill document' }))];
  return <div className="workspace-editor">
    <header><div><h1>{initial ? 'Workspace settings' : 'New workspace'}</h1><p className="muted">A shared directory, system prompt, and skills for your chats.</p></div><button onClick={onClose}>Close settings</button></header>
    {error && <div role="alert" className="error">{error}</div>}
    <p className="muted" role="status">{draft.skills.length} of 32 skills selected.</p>
    <form onSubmit={save}>
      <label>Workspace name<input aria-label="Workspace name" value={draft.name} required maxLength={120} onChange={event => change('name', event.target.value)} /></label>
      <label>{guestPaths ? 'Guest working directory' : 'Working directory'}<div className="field-with-button"><input aria-label="Working directory" value={draft.workingDirectory} required onChange={event => change('workingDirectory', event.target.value)} />{!guestPaths && <button type="button" onClick={browseDirectory}>Browse…</button>}</div></label>
      <BrowserSettings draft={draft} change={values => setDraft(previous => ({ ...previous, ...values }))} />
      <label>Execution detail<select aria-label="Execution detail" value={draft.executionDetail || 'standard'} onChange={event => change('executionDetail', event.target.value)}><option value="minimal">Minimal</option><option value="standard">Standard · tool calls and output</option><option value="full">Full · protocol events</option></select></label>
      <label>Diagnostic logging<select aria-label="Diagnostic logging" value={draft.diagnosticLogLevel || 'off'} onChange={event => change('diagnosticLogLevel', event.target.value)}><option value="off">Off</option><option value="errors">Errors</option><option value="info">Info</option><option value="debug">Debug · includes chat and tool content</option></select></label>
      <p className="muted">Debug writes prompts, responses, and tool payloads to local workspace logs. Diagnostic changes apply to subsequent tasks.</p>
      <label>System prompt<textarea aria-label="System prompt" value={draft.systemPrompt} placeholder="Instructions to apply to chats in this workspace…" onChange={event => change('systemPrompt', event.target.value)} /></label>
      <div className="settings-section-head"><h2>Assigned skills</h2><div className="skill-toolbar"><button type="button" disabled={!draft.skills.length} onClick={() => change('skills', [])}>Clear selected skills</button><button type="button" onClick={importSkills}>Add skill files…</button></div></div>
      <p className="muted">Choose personal or project skills, or add SKILL.md files from another folder. Relative references use each skill’s original directory.</p>
      <SkillList catalog={available} selected={draft.skills} onSelect={paths => change('skills', paths)} onPreview={setPreview} maxSelected={32} />
      {preview && <SkillPreview path={preview} onClose={() => setPreview(null)} />}
      <p className="muted">{draft.skills.length} skills selected. Changes apply to new tasks and follow-up turns. Running and paused tasks keep their current instructions.</p>
      <button className="primary" disabled={saving || draft.skills.length > 32}>{saving ? 'Saving…' : 'Save workspace'}</button>
    </form>
  </div>;
}
function App() {
  const [mobilePairing, setMobilePairing] = useState(false);
  const [rename, setRename] = useState(null);
  const renameTarget = useRef(null), titleEpoch = useRef(0);
  function closeRename() { renameTarget.current = null; setRename(null); }
  function requestRename(task) { const target = { id: task.conversationId, title: task.title || task.request.prompt }; renameTarget.current = target; setRename(target); }
  async function saveChatTitle(target, title) {
    const saved = await api.renameChat(target.id, title);
    titleEpoch.current++;
    if (renameTarget.current !== target) return;
    setTasks(previous => previous.map(task => task.conversationId === saved.id ? { ...task, title: saved.title } : task));
    setSelected(current => current?.conversationId === saved.id ? { ...current, title: saved.title } : current);
    closeRename();
  }
  const [sidebarWidth, setSidebarWidth] = useState(Math.max(220, Math.min(600, Number(localStorage.getItem('permissive-sidebar-width')) || 270)));
  const [workspaceHeight, setWorkspaceHeight] = useState(Math.max(60, Math.min(600, Number(localStorage.getItem('permissive-workspace-height')) || 180)));
  const [deletion, setDeletion] = useState(null), [deleting, setDeleting] = useState(false), [deleteError, setDeleteError] = useState('');
  const [historyAction, setHistoryAction] = useState(null), [historyBusy, setHistoryBusy] = useState(false), [historyError, setHistoryError] = useState('');
  const [versionState, setVersionState] = useState({ replyVersions: [], versionsBusy: false, branchPreparing: false });
  const branchChoiceEpoch = useRef(0);
  async function selectVersion(version) {
    if (historyWorking.current || versionState.versionsBusy) return;
    historyWorking.current = true; setHistoryBusy(true); setError('');
    const source = selectedConversation.current, draft = prompt;
    try {
      const chat = await api.chat(version.conversationId);
      if (selectedConversation.current !== source) return;
      openChat({ ...chat.tasks.at(-1), title: chat.title, chatGroupId: chat.chatGroupId });
      applyChat(chat); setPrompt(draft);
    } catch (error) { setError(error.message); }
    finally { historyWorking.current = false; setHistoryBusy(false); }
  }
  function chooseChat(task) {
    const epoch = ++branchChoiceEpoch.current;
    const saved = task.chatGroupId && localStorage.getItem(`permissive-selected-branch:${task.chatGroupId}`);
    if (!saved || saved === task.conversationId) { openChat(task); return; }
    api.chat(saved).then(chat => { if (epoch !== branchChoiceEpoch.current) return; openChat({ ...chat.tasks.at(-1), title: chat.title, chatGroupId: chat.chatGroupId }); applyChat(chat); })
      .catch(() => { if (epoch !== branchChoiceEpoch.current) return; localStorage.removeItem(`permissive-selected-branch:${task.chatGroupId}`); openChat(task); });
  }
  const [messageEdit, setMessageEdit] = useState(null), [detailsPanel, setDetailsPanel] = useState(null);
  const preservedScroll = useRef(null), replayingHistory = useRef(false);
  function preserveScroll() {
    if (!preservedScroll.current && transcriptElement.current) preservedScroll.current = { conversationId: selectedConversation.current, top: transcriptElement.current.scrollTop };
    followBottom.current = false;
    replayingHistory.current = true;
  }
  const historyPending = useRef(null), historyWorking = useRef(false);
  const [historyVersion, setHistoryVersion] = useState(0);
  async function changeHistory(action, point) {
    if (historyWorking.current) return;
    const signature = JSON.stringify([action, selected.conversationId, point.segmentId, point.taskId, point.messageId]);
    if (historyPending.current?.signature !== signature) historyPending.current = { ...point, signature, conversationId: selected.conversationId, submissionId: crypto.randomUUID() };
    const target = historyPending.current;
    if (action === 'edit') { setHistoryError(''); setMessageEdit({ ...target, text: point.text }); }
    else if (action === 'regenerate') await performHistoryAction(action, target);
    else if (['delete', 'delete-message'].includes(action)) { setHistoryError(''); setHistoryAction({ ...target, action }); }
    else await performHistoryAction('fork', target);
  }
  async function performHistoryAction(action, target) {
    if (historyWorking.current) return;
    historyWorking.current = true;
    setHistoryBusy(true); setHistoryError(''); setError('');
    if (action !== 'fork' && selectedConversation.current === target.conversationId) preserveScroll();
    try {
      const body = { taskId: target.taskId, messageId: target.messageId, submissionId: target.submissionId, ...(target.segmentId ? { segmentId: target.segmentId } : {}),
        ...(['delete', 'delete-message'].includes(action) ? { confirm: target.conversationId } : action === 'edit' ? { text: target.text } : {}) };
      const chat = await api[({ fork: 'forkChat', edit: 'editMessage', 'delete-message': 'deleteMessage', regenerate: 'regenerate' })[action] || 'deleteFrom'](target.conversationId, body);
      titleEpoch.current++; submission.current = null;
      if (chat.id !== target.conversationId && selectedConversation.current === target.conversationId) {
        const scroll = preservedScroll.current;
        const unsent = prompt;
        openChat({ ...chat.tasks.at(-1), title: chat.title, chatGroupId: chat.chatGroupId });
        applyChat(chat);
        if (action !== 'fork') {
          setPrompt(unsent);
          if (scroll) { preservedScroll.current = { ...scroll, conversationId: chat.id }; replayingHistory.current = true; followBottom.current = false; }
        }
      }
      else if (selectedConversation.current === target.conversationId) {
        preserveScroll();
        chatSnapshotCursor.current = 0; setEvents([]); applyChat(chat); setHistoryVersion(value => value + 1);
      }
      historyPending.current = null; setHistoryAction(null); setMessageEdit(null); expandedList.current = false; setTasks([]); await refresh();
    } catch (error) { replayingHistory.current = false; preservedScroll.current = null; if (action !== 'fork' && action !== 'regenerate') setHistoryError(error.message); else setError(error.message); }
    finally { historyWorking.current = false; setHistoryBusy(false); }
  }
  function resizeSidebar(value) { setSidebarWidth(value); localStorage.setItem('permissive-sidebar-width', value); }
  function resizeWorkspaces(value) { setWorkspaceHeight(value); localStorage.setItem('permissive-workspace-height', value); }
  function requestDeletion(kind, id, name) { setDeleteError(''); setDeletion({ kind, id, name }); }
  async function erase() {
    if (!deletion || deleting) return;
    setDeleting(true); setDeleteError('');
    try {
      const result = await api[deletion.kind === 'chat' ? 'deleteChat' : 'deleteWorkspace'](deletion.id);
      for (const id of result.conversationIds) localStorage.removeItem(`permissive-chat-options:${id}`);
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith('permissive-selected-branch:') && result.conversationIds.includes(localStorage.getItem(key))) localStorage.removeItem(key);
      }
      for (const id of [...result.taskIds || [], ...result.threadIds || []]) localStorage.removeItem(`permissive-chat-options:${id}`);
      for (const task of history) if (result.conversationIds.includes(task.conversationId)) {
        localStorage.removeItem(`permissive-chat-options:${task.threadId || task.id}`);
      }
      if (result.conversationIds.includes(selectedConversation.current) || deletion.kind === 'workspace' && workspaceId === deletion.id) {
        selectedConversation.current = null; setSelected(null); setHistory([]); setEvents([]); setQueue([]); setPrompt(''); setEditor(null);
      }
      expandedList.current = false; setTasks([]); setCursor(null); setDeletion(null); await refresh();
    } catch (error) { setDeleteError(error.message.includes('HTTP 409') ? 'Deletion could not finish (HTTP 409). Finish or cancel running and paused tasks, then retry. If some data was already removed, retry to finish overwriting it.' : error.message); }
    finally { setDeleting(false); }
  }
  const [ready, setReady] = useState(false), [error, setError] = useState('');
  const [guestPaths, setGuestPaths] = useState(false);
  const [tasks, setTasks] = useState([]), [selected, setSelected] = useState(null), [events, setEvents] = useState([]);
  const [history, setHistory] = useState([]), [queue, setQueue] = useState([]), [queueHeld, setQueueHeld] = useState(false);
  const [segment, setSegment] = useState(null), [olderSegments, setOlderSegments] = useState([]), [loadingSegment, setLoadingSegment] = useState(false);
  const prependScroll = useRef(null);
  async function loadPreviousSegment() {
    const previousId = (olderSegments[0]?.chat.segment || segment)?.previousId;
    if (!previousId || loadingSegment) return;
    const id = selectedConversation.current;
    setLoadingSegment(true);
    try {
      const chat = await api.chat(id, previousId), saved = [];
      let after = chat.segment?.after || 0;
      while (after < chat.eventCursor) {
        const page = await api.chatEvents(id, String(after), String(chat.eventCursor), previousId);
        saved.push(...page.data);
        const cursor = page.nextCursor ?? page.data.at(-1)?.id ?? chat.eventCursor;
        if (cursor <= after) break;
        after = cursor;
      }
      if (selectedConversation.current !== id) return;
      const element = transcriptElement.current;
      prependScroll.current = element ? { top: element.scrollTop, height: element.scrollHeight } : null;
      followBottom.current = false;
      setOlderSegments(current => current.some(value => value.chat.segment.id === previousId) ? current : [{ chat, events: saved }, ...current]);
    } catch (error) { setError(error.message); }
    finally { setLoadingSegment(false); }
  }
  const [chatView, setChatView] = useState(localStorage.getItem('permissive-chat-view') || 'workspace');
  const [sending, setSending] = useState(false);
  const selectedConversation = useRef(null), listScope = useRef(null), viewRef = useRef(chatView), submission = useRef(null);
  const chatSnapshotCursor = useRef(0);
  const transcriptElement = useRef(null), followBottom = useRef(true);
  const [prompt, setPrompt] = useState('');
  const [newChatRevision, setNewChatRevision] = useState(0);
  const [workspaces, setWorkspaces] = useState([]), [workspaceId, setWorkspaceId] = useState(localStorage.getItem('permissive-workspace') || ''), [editor, setEditor] = useState(null), [cursor, setCursor] = useState(null);
  const activeWorkspace = useRef(workspaceId), expandedList = useRef(false);
  const workspace = workspaces.find(value => value.id === workspaceId);
  const [now, setNow] = useState(Date.now()), [pending, setPending] = useState(null);
  function selectWorkspace(id) {
    branchChoiceEpoch.current++;
    activeWorkspace.current = id; localStorage.setItem('permissive-workspace', id);
    expandedList.current = false; setWorkspaceId(id); setTasks([]); setCursor(null); setSelected(null); setEvents([]); setEditor(null); setPrompt('');
    selectedConversation.current = null; setHistory([]); setQueue([]); setQueueHeld(false);
    refreshTasks(id).catch(error => setError(error.message));
  }
  async function refreshTasks(id = activeWorkspace.current, after = null) {
    if (!id && viewRef.current === 'workspace') return;
    const scope = viewRef.current === 'chronological' ? '*' : id;
    listScope.current = scope;
    const epoch = titleEpoch.current;
    const result = await api.chats(scope === '*' ? null : scope, after);
    if (epoch !== titleEpoch.current || scope !== listScope.current || (viewRef.current === 'workspace' && id !== activeWorkspace.current)) return;
    setTasks(previous => after || expandedList.current ? [...new Map([...previous, ...result.data].map(task => [task.chatGroupId || task.conversationId, task])).values()].sort((a, b) => b.lastActivityAt - a.lastActivityAt || b.conversationId.localeCompare(a.conversationId)) : result.data);
    if (after || !expandedList.current) setCursor(result.nextCursor);
  }
  async function refresh() {
    const values = (await api.workspaces()).data; setWorkspaces(values);
    if (!values.some(value => value.id === activeWorkspace.current)) selectWorkspace(values.find(value => value.name === 'General')?.id || values[0]?.id || '');
    else await refreshTasks();
  }
  async function saveWorkspace(id, settings) {
    const saved = await api.saveWorkspace(id, settings);
    setWorkspaces(previous => [...previous.filter(value => value.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name)));
    if (id === activeWorkspace.current) setEditor(null); else selectWorkspace(saved.id);
  }
  async function assignSkills(paths) {
    const saved = await api.saveWorkspace(workspace.id, { skills: [...new Set([...workspace.skills, ...paths])] });
    setWorkspaces(previous => previous.map(value => value.id === saved.id ? saved : value));
  }
  function updateSelected(task) {
    setSelected(current => current?.id === task.id ? { ...current, ...task } : current);
    setTasks(previous => previous.map(current => current.conversationId === task.conversationId ? { ...current, ...task } : current));
    setHistory(previous => previous.map(current => current.id === task.id ? { ...current, ...task } : current));
  }
  function applyChat(chat, epoch = titleEpoch.current) {
    if (selectedConversation.current !== chat.id) return;
    if (chat.eventCursor < chatSnapshotCursor.current) return;
    chatSnapshotCursor.current = chat.eventCursor;
    setVersionState({ replyVersions: chat.replyVersions || [], versionsBusy: !!chat.versionsBusy, branchPreparing: !!chat.branchPreparing, error: chat.branchPreparationError });
    if (chat.chatGroupId) localStorage.setItem(`permissive-selected-branch:${chat.chatGroupId}`, chat.id);
    setSegment(chat.segment || null);
    setHistory(chat.tasks);
    setSelected(current => ({ ...chat.tasks.at(-1), chatGroupId: chat.chatGroupId, title: epoch === titleEpoch.current ? chat.title ?? current?.title : current?.title }));
    if (chat.title != null && epoch === titleEpoch.current) setTasks(previous => previous.map(task => task.conversationId === chat.id ? { ...task, title: chat.title } : task));
    setQueue(chat.followups); setQueueHeld(chat.queueHeld);
  }
  async function refreshChat(id, isLive) { const epoch = titleEpoch.current; const chat = await api.chat(id); if (isLive()) applyChat(chat, epoch); }
  function openChat(task) {
    branchChoiceEpoch.current++;
    if (selectedConversation.current === task.conversationId) { setEditor(null); return; }
    chatSnapshotCursor.current = 0;
    setSegment(null); setOlderSegments([]);
    setVersionState({ replyVersions: [], versionsBusy: false, branchPreparing: false });
    followBottom.current = true;
    preservedScroll.current = null; replayingHistory.current = false; setDetailsPanel(null); setMessageEdit(null);
    selectedConversation.current = task.conversationId;
    activeWorkspace.current = task.workspaceId; setWorkspaceId(task.workspaceId); localStorage.setItem('permissive-workspace', task.workspaceId);
    setEditor(null); setSelected(task); setHistory([]); setEvents([]); setQueue([]); setPrompt(''); setError('');
  }
  function changeView(value) {
    viewRef.current = value; listScope.current = value === 'chronological' ? '*' : activeWorkspace.current;
    setChatView(value); localStorage.setItem('permissive-chat-view', value); expandedList.current = false; setTasks([]); setCursor(null);
    refreshTasks().catch(error => setError(error.message));
  }
  useEffect(() => {
    closeRename();
  }, [workspaceId, selected?.conversationId]);
  useEffect(() => {
    if (!selected) { setOlderSegments([]); setSegment(null); setLoadingSegment(false); }
  }, [selected?.conversationId]);
  useEffect(() => {
    api.onReady(() => { setReady(true); refresh().catch(error => setError(error.message)); });
    api.onError(setError);
    api.health().then(value => { setGuestPaths(value.executionRole === 'controller'); setReady(true); refresh(); }).catch(() => {});
    const timer = setInterval(() => setNow(Date.now()), 1000);
    const refreshTimer = setInterval(() => refresh().catch(() => {}), 5000);
    return () => { clearInterval(timer); clearInterval(refreshTimer); };
  }, []);
  useEffect(() => {
    if (!selected) return;
    const id = selected.conversationId;
    let live = true;
    let lastEvent = 0;
    const connected = api.onWatchConnected(value => {
      if (live && value.conversationId === id) setError(previous => previous === 'Chat connection interrupted. Reconnecting…' ? '' : previous);
    });
    const unsubscribe = api.onEvent(value => {
      if (!live || value.conversationId !== id) return;
      lastEvent = Math.max(lastEvent, value.event.id);
      setEvents(previous => previous.some(event => event.id === value.event.id) ? previous : [...previous, value.event]);
      if (value.event.kind === 'history') {
        if (!replayingHistory.current) preserveScroll();
        chatSnapshotCursor.current = 0; setEvents([]); setHistoryVersion(value => value + 1);
      } else if (['status', 'queue', 'steering', 'recovery'].includes(value.event.kind)) refreshChat(id, () => live).catch(error => { if (live) setError(error.message); });
    });
    async function loadAndWatch() {
      try {
        const epoch = titleEpoch.current;
        const chat = await api.chat(id);
        if (!live) return;
        applyChat(chat, epoch);
        lastEvent = chat.segment?.after || 0;
        const saved = [];
        while (lastEvent < chat.eventCursor) {
          const page = await api.chatEvents(id, String(lastEvent), String(chat.eventCursor), chat.segment?.id);
          if (!live) return;
          saved.push(...page.data);
          const cursor = page.nextCursor ?? page.data.at(-1)?.id ?? chat.eventCursor;
          if (cursor <= lastEvent) break;
          lastEvent = cursor;
        }
        if (!live) return;
        replayingHistory.current = false;
        setEvents(saved);
        while (live) {
          try { const result = await api.watch(id, lastEvent, true); if (result?.cancelled || !live) return; }
          catch (error) { if (!live) return; setError('Chat connection interrupted. Reconnecting…'); }
          if (live) await new Promise(resolve => setTimeout(resolve, 1000));
        }
      } catch (error) { if (live) setError(error.message); }
    }
    loadAndWatch();
    const poll = setInterval(() => { if (live) refreshChat(id, () => live).catch(() => {}); }, 2000);
    return () => { live = false; clearInterval(poll); unsubscribe(); connected(); api.unwatch(); };
  }, [selected?.conversationId, historyVersion]);
  useLayoutEffect(() => {
    const element = transcriptElement.current, saved = preservedScroll.current;
    if (element && prependScroll.current) {
      element.scrollTop = prependScroll.current.top + element.scrollHeight - prependScroll.current.height;
      prependScroll.current = null;
    } else if (element && saved?.conversationId === selectedConversation.current) {
      element.scrollTop = Math.min(saved.top, Math.max(0, element.scrollHeight - element.clientHeight));
      if (!replayingHistory.current) preservedScroll.current = null;
    } else if (followBottom.current && element) element.scrollTop = element.scrollHeight;
  }, [events, history, queue, olderSegments]);
  async function control(action) {
    const id = selected.id;
    setPending({ id, action }); setError('');
    if (action === 'cancel') setSelected(current => current?.id === id ? { ...current, status: 'cancelling' } : current);
    try { updateSelected(await api[action](id)); await refresh(); }
    catch (error) { setError(error.message); api.get(id).then(updateSelected).catch(() => {}); }
    finally { setPending(null); }
  }
  async function submit(settings, action = 'continue', attachments = []) {
    setError('');
    if (sending) return false;
    const conversation = selected?.conversationId || null;
    const message = { prompt: prompt.trim() ? prompt : 'Please examine the attached files.', ...(attachments.length ? { attachments } : {}) };
    const signature = JSON.stringify({ conversation, ...message, settings: action === 'steer' ? null : settings, action });
    if (submission.current?.signature !== signature) submission.current = { signature, id: crypto.randomUUID(), turnId: selected?.activeTurnId, taskId: selected?.id };
    const submissionId = submission.current.id;
    followBottom.current = true;
    setSending(true);
    try {
      if (action === 'steer') {
        await api.steer(submission.current.taskId, { ...message, expectedTurnId: submission.current.turnId, submissionId });
      } else if (action === 'queue') {
        const epoch = titleEpoch.current;
        const chat = await api.queueFollowup(conversation, { ...message, ...settings, submissionId }); applyChat(chat, epoch);
      } else {
        const task = selected ? await api.continue(submission.current.taskId, message.prompt, { ...message, ...settings, submissionId })
          : await api.submit({ ...message, workspaceId, ...settings, submissionId });
        if (selectedConversation.current === conversation) {
          if (!conversation) chatSnapshotCursor.current = 0;
          selectedConversation.current = task.conversationId; setSelected(task);
          setHistory(previous => previous.some(value => value.id === task.id) ? previous : [...previous, task]);
        }
      }
      if (selectedConversation.current === conversation || !conversation && selectedConversation.current && activeWorkspace.current === workspaceId) setPrompt('');
      submission.current = null; await refresh();
      return true;
    } catch (error) { setError(action === 'steer' ? `${error.message}. Message delivery was not confirmed; your draft is retained.` : error.message); return false; }
    finally { setSending(false); }
  }
  const elapsed = elapsedSeconds(selected, now);
  const telemetry = taskTelemetry(selected, events.filter(event => event.taskId === selected?.id), now);
  const busy = pending?.id === selected?.id;
  return <main style={{ '--sidebar-width': `${sidebarWidth}px`, '--workspace-height': `${workspaceHeight}px` }}>
    <aside><div className="brand">Codex <strong>Permissive</strong></div><p className="muted">DeepSeek V4.1 Flash · Sparky</p>
      <button className={editor?.mode === 'skills' ? 'active library-button' : 'library-button'} onClick={() => setEditor({ mode: 'skills' })}>Skills</button>
      <div className="sidebar-heading"><h2>Workspaces</h2><button aria-label="New workspace" onClick={() => { setEditor({ mode: 'new' }); setSelected(null); }}>＋</button></div>
      <div className="workspace-list">{workspaces.map(value => <div className="workspace-row" key={value.id}>
        <button className={workspaceId === value.id ? 'workspace active' : 'workspace'} onClick={() => selectWorkspace(value.id)} aria-pressed={workspaceId === value.id}><span>{value.name}</span><small>{value.taskCount} chats</small></button>
        <button className="workspace-settings" aria-label={`Workspace settings for ${value.name}`} onClick={() => { selectWorkspace(value.id); setEditor({ mode: 'edit', initial: value }); }}>⚙</button>
        <button className="delete-button" aria-label={`Securely delete workspace ${value.name}`} onClick={() => requestDeletion('workspace', value.id, value.name)}>×</button>
      </div>)}</div>
      <Splitter label="Resize Workspaces and Chats" orientation="horizontal" value={workspaceHeight} min={60} max={Math.max(60, Math.min(600, window.innerHeight - 420))} onChange={resizeWorkspaces} />
      <div className="sidebar-heading"><h2>Chats</h2></div>
      <select aria-label="Chat view" value={chatView} onChange={event => changeView(event.target.value)}><option value="workspace">Workspace</option><option value="chronological">Chronological · all chats</option></select>
      <button disabled={!workspace} onClick={() => { selectedConversation.current = null; setHistory([]); setQueue([]); setQueueHeld(false); setSelected(null); setEvents([]); setEditor(null); setPrompt(''); setNewChatRevision(previous => previous + 1); }}>＋ New task</button>
      <div className="task-list">{tasks.map(task => <div className="chat-row" key={task.chatGroupId || task.conversationId}><button className={(selected?.chatGroupId || selected?.conversationId) === (task.chatGroupId || task.conversationId) ? 'task active' : 'task'}
        onClick={() => chooseChat(task)} onContextMenu={event => { event.preventDefault(); api.taskMenu(task.id).catch(error => setError(error.message)); }}>
        <span>{(task.title || task.request.prompt).slice(0, 72)}</span><small>{statusLabel(task.status)}{chatView === 'chronological' ? ` · ${workspaces.find(value => value.id === task.workspaceId)?.name || 'Workspace'}` : ''}</small></button><button className="rename-button" aria-label={`Rename chat ${(task.title || task.request.prompt).slice(0, 72)}`} onClick={() => requestRename(task)}>✎</button><button className="delete-button" aria-label={`Securely delete chat ${(task.title || task.request.prompt).slice(0, 72)}`} onClick={() => requestDeletion('chat', task.conversationId, task.title || task.request.prompt)}>×</button></div>)}
        {cursor && <button onClick={async () => { try { expandedList.current = true; await refreshTasks(workspaceId, cursor); } catch (error) { setError(error.message); } }}>Load more chats</button>}
        {!tasks.length && <p className="muted">{chatView === 'chronological' ? 'No chats yet.' : 'No chats in this workspace yet.'}</p>}</div>
      <button onClick={() => setMobilePairing(true)}>Pair Android</button>
      <div className="connection">{ready ? '● Service connected' : '○ Starting service…'}</div></aside>
    <Splitter label="Resize sidebar" orientation="vertical" value={sidebarWidth} min={220} max={Math.max(220, Math.min(600, window.innerWidth - 420))} onChange={resizeSidebar} />
    <section>{editor?.mode === 'skills' ? <SkillLibrary workspace={workspace} onAssign={assignSkills} onClose={() => setEditor(null)} /> : editor ? <WorkspaceEditor key={editor.initial?.id || 'new'} initial={editor.initial} guestPaths={guestPaths} onSave={saveWorkspace} onClose={() => setEditor(null)} /> : <><header><div className="conversation-heading"><p className="workspace-caption">{workspace?.name || 'Select a workspace'}</p><h1>{selected ? selected.title || tasks.find(task => task.conversationId === selected.conversationId)?.title || history[0]?.request.prompt || 'Task conversation' : 'What should we work on?'}</h1>
      {!selected && <p className="muted">{guestPaths ? 'Code execution and browser work in your sandbox' : 'Code execution and browser work on Windows'}</p>}</div>
      {selected && <button onClick={() => requestRename({ ...selected, title: selected.title || tasks.find(task => task.conversationId === selected.conversationId)?.title || history[0]?.request.prompt })}>Rename chat</button>}
      {selected && !terminal.has(selected.status) && <div className="task-controls">
        {['queued', 'running'].includes(selected.status) && <button disabled={busy} onClick={() => control('pause')}>Pause task</button>}
        {['paused', 'pausing'].includes(selected.status) && <button disabled={busy} onClick={() => control('resume')}>Resume task</button>}
        <button className="stop" disabled={selected.status === 'cancelling' || pending?.action === 'cancel' && busy} onClick={() => control('cancel')}>{selected.status === 'cancelling' ? 'Cancelling…' : 'Cancel task'}</button>
      </div>}
      {selected?.status === 'failed' && selected.canRetry && <div className="task-controls"><button disabled={busy} onClick={() => control('retry')}>{busy && pending.action === 'retry' ? 'Retrying response…' : 'Retry response'}</button></div>}</header>
      {error && <div role="alert" className="error">{error}</div>}
      <div className="chat-status-row">
        {selected && <><span role="status" className={`task-status status-${selected.status}`}>{statusLabel(selected.status)}{selected.status === 'running' && ` (~${telemetry.tokensPerSecond.toFixed(1)} TPS)`}</span><small>{Math.floor(elapsed / 60)}m {elapsed % 60}s</small><button onClick={() => setDetailsPanel('Stats')}>Stats</button><button onClick={() => setDetailsPanel('Activity')}>Activity</button></>}
        <ConversationAttachments key={`attachments:${selected?.conversationId || workspaceId}`} tasks={history} events={events} />
      </div>
      <div className="conversation" ref={transcriptElement} onScroll={event => {
        const element = event.currentTarget;
        if (!preservedScroll.current && !replayingHistory.current) followBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
      }}>
        {((olderSegments[0]?.chat.segment || segment)?.number > 1) && <div className="compaction-boundary"><span>Compacted context · Segment {(olderSegments[0]?.chat.segment || segment).number}</span><button disabled={loadingSegment} onClick={loadPreviousSegment}>{loadingSegment ? 'Loading previous segment…' : 'Load previous segment'}</button></div>}
        {olderSegments.map(value => <React.Fragment key={value.chat.segment.id}><Transcript tasks={value.chat.tasks} events={value.events} detail={workspace?.executionDetail || 'standard'} now={now} historyBusy={historyBusy || sending} onAction={changeHistory} replyVersions={value.chat.replyVersions} versionsBusy={versionState.versionsBusy} onVersion={selectVersion} /><div className="compaction-boundary">Compacted context · Segment {value.chat.segment.number + 1}</div></React.Fragment>)}
        <Transcript tasks={history} events={events} detail={workspace?.executionDetail || 'standard'} now={now} historyBusy={historyBusy || sending} onAction={changeHistory} {...versionState} onVersion={selectVersion} />
        {versionState.branchPreparing && <p role="status">Preparing response history…{versionState.error ? ' Retrying saved preparation.' : ''}</p>}
        {selected && !history.length && <p className="muted">Loading conversation…</p>}
        {queue.length > 0 && <div className="followup-queue"><strong>{queueHeld ? 'Follow-ups held' : 'Queued follow-ups'}</strong>
          {queue.map(item => <div className="queued-followup" key={item.id}><span>{item.body.prompt}</span><button onClick={() => { const epoch = titleEpoch.current; api.removeFollowup(selected.conversationId, item.id).then(chat => applyChat(chat, epoch)).catch(error => setError(error.message)); }}>Remove</button></div>)}
          {queueHeld && <button onClick={() => { const epoch = titleEpoch.current; api.resumeQueue(selected.conversationId).then(chat => applyChat(chat, epoch)).catch(error => setError(error.message)); }}>Resume queue</button>}
        </div>}
      </div>
      <ChatComposer key={selected?.conversationId || `new:${workspaceId}:${newChatRevision}`} workspace={workspace} task={selected} prompt={prompt} setPrompt={setPrompt} disabled={!ready || !workspace || sending || historyBusy} onSubmit={submit} /></>}</section>
    {historyAction && <div className="table-modal-backdrop"><div className="table-modal-panel delete-dialog" role="dialog" aria-modal="true" aria-label={historyAction.action === 'delete-message' ? 'Delete message' : 'Delete from here'}>
      <h2>{historyAction.action === 'delete-message' ? 'Delete only this message?' : 'Delete this message and everything below it?'}</h2><p>{historyAction.action === 'delete-message' ? 'Delete this message? Other messages stay in the chat.' : 'Earlier messages stay. This message, later messages, and queued follow-ups will be removed.'}</p>
      {historyError && <p className="error" role="alert">{historyError}</p>}
      <div className="table-modal-actions"><button autoFocus disabled={historyBusy} onClick={() => setHistoryAction(null)}>Cancel</button><button className="stop" disabled={historyBusy} onClick={() => performHistoryAction(historyAction.action, historyAction)}>{historyBusy ? 'Working…' : historyAction.action === 'delete-message' ? 'Delete message' : 'Delete from here'}</button></div>
    </div></div>}
    {messageEdit && <div className="table-modal-backdrop"><form className="table-modal-panel" role="dialog" aria-modal="true" aria-label="Edit message" onSubmit={event => { event.preventDefault(); performHistoryAction('edit', messageEdit); }}>
      <h2>Edit message</h2><p>Later messages stay in this chat. Future responses use your revised text.</p>
      <textarea autoFocus aria-label="Message text" value={messageEdit.text} disabled={historyBusy} onChange={event => setMessageEdit(previous => ({ ...previous, text: event.target.value, submissionId: crypto.randomUUID() }))} />
      {historyError && <p role="alert" className="error">{historyError}</p>}
      <div className="table-modal-actions"><button type="button" disabled={historyBusy} onClick={() => setMessageEdit(null)}>Cancel</button><button className="primary" disabled={historyBusy || !messageEdit.text.trim()}>{historyBusy ? 'Saving…' : 'Save message'}</button></div>
    </form></div>}
    {detailsPanel && <div className="table-modal-backdrop"><div className="table-modal-panel" role="dialog" aria-modal="true" aria-label={detailsPanel}><header><h2>{detailsPanel}</h2><button autoFocus onClick={() => setDetailsPanel(null)}>Close</button></header><div className="chat-details-scroll"><ConversationDetails kind={detailsPanel} tasks={history} events={events} now={now} detail={workspace?.executionDetail || 'standard'} /></div></div></div>}
    {deletion && <div className="table-modal-backdrop"><div className="table-modal-panel delete-dialog" role="dialog" aria-modal="true" aria-label="Secure deletion">
      <h2>Securely delete {deletion.kind}</h2><p>{deletion.name}</p>
      <p>Overwrites saved app data seven times and permanently removes {deletion.kind === 'workspace' ? 'this workspace and all its chats' : 'this entire chat'}. Finish or cancel running tasks first. Files in your working directory stay intact.</p>
      <p className="muted">Seven file overwrites cannot guarantee erasure of SSD remnants, snapshots or backups. System and desktop caches, browser profiles and exported files are outside the overwrite operation.</p>
      {deleteError && <p role="alert" className="error">{deleteError}</p>}
      <div className="table-modal-actions"><button autoFocus disabled={deleting} onClick={() => setDeletion(null)}>Keep {deletion.kind}</button><button className="stop" disabled={deleting} onClick={erase}>{deleting ? 'Overwriting…' : 'Delete permanently · 7 passes'}</button></div>
    </div></div>}
    {rename && <RenameChat key={rename.id} initialTitle={rename.title} onSave={title => saveChatTitle(rename, title)} onClose={closeRename} />}
    {mobilePairing && <MobilePairing onClose={() => setMobilePairing(false)} />}
  </main>;
}
createRoot(document.getElementById('root')).render(<App />);
