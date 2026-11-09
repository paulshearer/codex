import React, { useEffect, useLayoutEffect, useRef } from 'react';
import { Markdown } from './markdown.jsx';
import { activitySentence, conversationTimeline, executionItems, terminalStatuses, taskProgress, taskRecovery, taskThinking, taskTelemetry, tokenFields } from './conversation.mjs';

function ThinkingStep({ step, active, status }) {
  const output = useRef(null), follow = useRef(true);
  useLayoutEffect(() => {
    if (output.current && follow.current) output.current.scrollTop = output.current.scrollHeight;
  }, [step.text, step.summary]);
  if (!active) return <div className="thinking-summary"><p>{activitySentence(step.summary || step.text)}</p>
    <details className="thinking-transcript"><summary>View streamed thinking</summary><pre>{step.text || step.summary}</pre></details>
  </div>;
  return <div className="live-progress"><span>{status === 'running' ? 'Thinking · live' : `Thinking · ${status}`}</span>
    <pre className="live-thinking-text" ref={output} tabIndex={0} aria-label="Live model thinking" onScroll={event => {
      const element = event.currentTarget; follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 32;
    }}>{step.text || step.summary}</pre>
  </div>;
}

function Thinking({ task, events, complete }) {
  const thinking = taskThinking(events);
  return <>{thinking.steps.map((step, index) => <ThinkingStep key={step.id} step={step} status={task.status} active={!complete && !step.complete && index === thinking.steps.length - 1} />)}
    {!complete && <div className="live-progress"><span>{({ running: thinking.steps.length ? 'Current activity' : 'Thinking · live', queued: 'Queued', paused: 'Paused', pausing: 'Pausing', cancelling: 'Cancelling' })[task.status] || task.status}</span>
    <p role="status">{task.status === 'paused' ? 'Paused. Ready to resume.' : task.status === 'running' ? taskProgress(events) : ''}</p>
    </div>}</>;
}

function ExecutionDetails({ block, full }) {
  const text = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2);
  const items = executionItems(block.events);
  const labels = { fileChange: 'file changes', webSearch: 'web search', mcpToolCall: 'tool call', dynamicToolCall: 'tool call', imageView: 'image review' };
  const summary = items.map(item => activitySentence(item.command ? `${item.status === 'failed' || item.exitCode > 0 ? 'Command failed' : item.status === 'completed' ? 'Ran' : 'Running'}: ${item.command}` : `${item.status === 'completed' ? 'Completed' : item.status === 'failed' ? 'Failed' : 'Activity'}: ${item.tool || labels[item.type] || 'tool activity'}`)).join(' ');
  return <details className="execution-details"><summary>{activitySentence(summary, 'Tool activity is available.')} <small>Execution details ({block.events.length} {block.events.length === 1 ? 'event' : 'events'})</small></summary>
    {full ? block.events.map(event => <details key={event.id}><summary>{event.data.method}</summary><pre>{JSON.stringify(event.data.params, null, 2)}</pre></details>)
      : executionItems(block.events).map(item => <div className="execution-item" key={item.id}>
        <strong>{item.command || item.tool || item.label || ({ fileChange: 'File changes', webSearch: 'Web search', mcpToolCall: 'Tool call' }[item.type]) || item.type}</strong>
        <pre>{text(item.aggregatedOutput ?? item.output)}</pre>
        {item.arguments && <pre>{JSON.stringify(item.arguments, null, 2)}</pre>}
        {item.result && <pre>{JSON.stringify(item.result, null, 2)}</pre>}
        {item.changes && <pre>{JSON.stringify(item.changes, null, 2)}</pre>}
        {item.action && <pre>{JSON.stringify(item.action, null, 2)}</pre>}
        {item.path && <pre>{item.path}</pre>}
        {item.status && <small>{item.status}{item.exitCode != null ? ` · Exit ${item.exitCode}` : ''}</small>}
      </div>)}
  </details>;
}

function MessageActions({ task, messageId, text, complete = true, disabled, onAction }) {
  function positionMenu(event) {
    const details = event.currentTarget;
    if (!details.open) return;
    const anchor = details.querySelector('summary').getBoundingClientRect(), menu = details.querySelector('[role="menu"]');
    menu.style.maxHeight = `${Math.max(80, window.innerHeight - 16)}px`;
    const bounds = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 8))}px`;
    menu.style.top = `${anchor.bottom + bounds.height + 8 <= window.innerHeight ? anchor.bottom + 4 : Math.max(8, anchor.top - bounds.height - 4)}px`;
    menu.style.visibility = 'visible';
  }
  function act(event, action) {
    event.currentTarget.closest('details').open = false;
    onAction(action, { taskId: task.id, segmentId: task.segmentId, messageId, text, user: messageId === 'prompt' });
  }
  return <details className="message-actions" onToggle={positionMenu} onKeyDown={event => { if (event.key === 'Escape') { event.currentTarget.open = false; event.currentTarget.querySelector('summary').focus(); } }}><summary aria-label="Message actions" title="Message actions">⋮</summary>
    <div role="menu"><button role="menuitem" disabled={disabled || !complete} onClick={event => act(event, 'edit')}>Edit</button>
      <button role="menuitem" className="danger-text" disabled={disabled || !complete} onClick={event => act(event, 'delete-message')}>Delete</button>
      <button role="menuitem" disabled={disabled || !complete} onClick={event => act(event, 'regenerate')}>Regenerate</button>
      <button role="menuitem" onClick={event => { event.currentTarget.closest('details').open = false; navigator.clipboard.writeText(text).catch(() => {}); }}>Copy</button>
      <button role="menuitem" disabled={disabled || !complete} onClick={event => act(event, 'fork')}>Fork from here</button>
      <button role="menuitem" className="danger-text" disabled={disabled} onClick={event => act(event, 'delete')}>Delete from here</button></div>
  </details>;
}

export function ReplyControls({ choice, disabled, onVersion }) {
  if (!choice || choice.versions.length < 2) return null;
  const index = choice.versions.findIndex(version => version.id === choice.selectedVersionId);
  return <div className="reply-versions" role="group" aria-label="Response versions">
    <button aria-label="Previous response" disabled={disabled || index <= 0} onClick={() => onVersion(choice.versions[index - 1])}>‹</button>
    <span>Reply {index + 1} of {choice.versions.length}</span>
    <button aria-label="Next response" disabled={disabled || index < 0 || index >= choice.versions.length - 1} onClick={() => onVersion(choice.versions[index + 1])}>›</button>
  </div>;
}

export function Transcript({ tasks, events, detail, now, onAction, historyBusy, replyVersions = [], versionsBusy = false, onVersion }) {
  useEffect(() => {
    const closeOutside = event => document.querySelectorAll('.message-actions[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, []);
  const disabled = historyBusy || versionsBusy || tasks.some(task => !terminalStatuses.has(task.status));
  return tasks.map(task => {
    const taskEvents = events.filter(event => event.taskId === task.id), timeline = conversationTimeline(task, taskEvents, detail);
    if (!task.request.prompt && !task.request.attachments?.length && !timeline.length && !task.result) return null;
    const complete = terminalStatuses.has(task.status), recovery = taskRecovery(taskEvents, task.status);
    return <div className="conversation-turn" data-task-id={task.id} key={task.id}>
      {(task.request.prompt || task.request.attachments?.length > 0) && <div className="prompt"><div className="message-heading"><small>You</small>{onAction && <MessageActions task={task} messageId="prompt" text={task.request.prompt} disabled={disabled} onAction={onAction} />}</div><div>{task.request.prompt}</div></div>}
      {timeline.map(item => item.type === 'details' ? null
        : <div className={item.type === 'user' ? 'prompt' : 'answer'} key={`${item.type}:${item.id}`}><div className="message-heading"><small>{item.type === 'user' ? 'You · steering' : 'Codex Permissive'}</small>{onAction && <MessageActions task={task} messageId={item.messageId} text={item.text} complete={item.complete} disabled={disabled} onAction={onAction} />}</div>{item.type === 'user' ? <div>{item.text}</div> : <Markdown>{item.text}</Markdown>}<ReplyControls choice={replyVersions.find(choice => choice.taskId === task.id && choice.messageId === item.messageId)} disabled={disabled} onVersion={onVersion} /></div>)}
      {!timeline.some(item => item.type === 'assistant') && task.result && <div className="answer"><div className="message-heading"><small>Codex Permissive</small>{onAction && <MessageActions task={task} messageId="response" text={task.result} disabled={disabled} onAction={onAction} />}</div><Markdown>{task.result}</Markdown><ReplyControls choice={replyVersions.filter(choice => choice.taskId === task.id).at(-1)} disabled={disabled} onVersion={onVersion} /></div>}
      {!task.result && !timeline.some(item => item.type === 'assistant') && replyVersions.filter(choice => choice.taskId === task.id).map(choice => <ReplyControls key={choice.groupId} choice={choice} disabled={disabled} onVersion={onVersion} />)}
      {!complete && <Thinking task={task} events={taskEvents} complete={complete} />}
      {recovery && <div className="pause-notice recovery-notice" role="status"><strong>{recovery.title}</strong>{recovery.message && <p>{recovery.message}</p>}</div>}
      {task.status === 'paused' && <p className="pause-notice">Paused. Select Resume task to continue.</p>}
      {task.error && <div className="error">{task.error}</div>}
      {task.goal && <p className="goal-status" role="status">Goal: {task.goal.status} · {task.goal.objective}</p>}
    </div>;
  });
}

export function ConversationDetails({ kind, tasks, events, now, detail }) {
  return tasks.map((task, index) => {
    const taskEvents = events.filter(event => event.taskId === task.id), telemetry = taskTelemetry(task, taskEvents, now);
    return <div key={task.id}><h3>Turn {index + 1}</h3>{kind === 'Stats' ? <div className="token-usage"><p>TTFT {telemetry.ttft == null ? '—' : `${telemetry.ttft.toFixed(2)}s`} · Last Token Sec ~{telemetry.tokensPerSecond.toFixed(1)} tokens/s (2s stream estimate) · Compute time {telemetry.computeSeconds.toFixed(1)}s</p>{telemetry.usage ? <pre>{JSON.stringify(Object.fromEntries(tokenFields.map(key => [key, telemetry.usage[key] ?? null])), null, 2)}</pre> : <p>Token usage was not reported for this task.</p>}</div> : <><Thinking task={task} events={taskEvents} complete={terminalStatuses.has(task.status)} />{conversationTimeline(task, taskEvents, detail).filter(item => item.type === 'details').map(item => <ExecutionDetails key={item.id} block={item} full={detail === 'full'} />)}{!taskEvents.length && <p>No activity recorded.</p>}</>}</div>;
  });
}
