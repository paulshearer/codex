export function assistantMessages(events) {
  const messages = new Map(), seen = new Set();
  for (const event of events) {
    if (seen.has(event.id) || event.kind !== 'agent') continue;
    seen.add(event.id);
    const { method, params = {} } = event.data;
    if (method === 'item/agentMessage/delta') {
      const id = params.itemId;
      const previous = messages.get(id) || { id, text: '', complete: false };
      if (!previous.complete) messages.set(id, { ...previous, text: previous.text + (params.delta || '') });
    } else if (method === 'item/completed' && params.item?.type === 'agentMessage') {
      const item = params.item;
      messages.set(item.id, { id: item.id, text: item.text || '', complete: true, phase: item.phase });
    }
  }
  return [...messages.values()].filter(message => message.text.trim());
}

export function statusLabel(status) {
  return ({ queued: 'Queued', running: 'Running', pausing: 'Pausing after the current activity…',
    paused: 'Paused', cancelling: 'Cancelling…', cancelled: 'Cancelled', completed: 'Done',
    failed: 'Failed', interrupted: 'Interrupted' })[status] || status;
}

export const terminalStatuses = new Set(['completed', 'cancelled', 'failed', 'interrupted']);
export const tokenFields = ['cachedInputTokens', 'inputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens'];

export function activitySentence(text, fallback = 'Completed a thinking step.') {
  const plain = (text || '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/\*\*(\S(?:[^*]*?\S)?)\*\*/g, '$1').replace(/`/g, '').replace(/^\s*#{1,6}\s+/, '').replace(/\s+/g, ' ').trim();
  if (!plain) return fallback;
  const sentence = plain.match(/^.*?[.!?](?=\s|$)/)?.[0] || plain;
  if (sentence.length > 220) return `${sentence.slice(0, 217).trimEnd()}…`;
  return /[.!?…]$/.test(sentence) ? sentence : `${sentence}.`;
}

export function conversationAttachments(tasks, events) {
  const files = new Map();
  function inputs(ids, taskId) {
    for (const value of ids || []) {
      const id = typeof value === 'string' ? value : value.id;
      files.set(`attachment:${id}`, { ...files.get(`attachment:${id}`), ...(typeof value === 'object' ? value : {}), id, taskId, input: true });
    }
  }
  for (const task of tasks) {
    inputs(task.request?.attachments, task.id);
    for (const artifact of task.artifacts || []) files.set(`artifact:${task.id}:${artifact.id}`, { ...artifact, taskId: task.id, input: false });
  }
  for (const event of events) if (event.kind === 'steering') inputs(event.data.attachments, event.taskId);
  return [...files.values()];
}

export function taskRecovery(events, status) {
  let recovery = null, recoveryId = 0;
  const seen = new Set(), titles = { requested: 'Response retry requested', retrying: 'Retrying response',
    recovered: 'Response recovered', exhausted: 'Automatic response retries exhausted',
    history: 'Restoring conversation', history_ready: 'Conversation restored' };
  for (const event of events) {
    if (seen.has(event.id) || event.kind !== 'recovery' || !titles[event.data?.phase]) continue;
    seen.add(event.id);
    const { phase, attempt, maxAttempts, message } = event.data;
    const count = phase === 'retrying' && Number.isInteger(attempt) && attempt > 0 && Number.isInteger(maxAttempts) && maxAttempts >= attempt
      ? ` · ${attempt} of ${maxAttempts}` : '';
    recovery = { phase, title: titles[phase] + count, message: typeof message === 'string' ? message : '' };
    recoveryId = event.id;
  }
  if (!recovery || ['history', 'history_ready', 'recovered'].includes(recovery.phase)) return null;
  if (status && recovery.phase === 'exhausted' && status !== 'failed') return null;
  if (recovery.phase !== 'exhausted' && terminalStatuses.has(status)) return null;
  if (recovery.phase !== 'exhausted' && events.some(event => event.id > recoveryId && event.kind === 'agent' &&
    (event.data?.method?.startsWith('item/') || event.data?.method === 'turn/started'))) return null;
  return recovery;
}

export function elapsedSeconds(task, now = Date.now()) {
  if (!task) return 0;
  const end = terminalStatuses.has(task.status) ? task.completedAt ?? task.updatedAt ?? task.createdAt
    : task.status === 'paused' ? task.updatedAt : now / 1000;
  return Math.max(0, Math.floor(end - task.createdAt));
}

// Status remains separate from the actual model text shown by taskThinking.
export function taskProgress(events) {
  let text = '', summaryId = null;
  const seen = new Set();
  for (const event of events) {
    if (seen.has(event.id)) continue;
    if (event.kind === 'recovery' && ['history', 'history_ready'].includes(event.data?.phase)) {
      seen.add(event.id); text = event.data.phase === 'history' ? 'Preparing response…' : ''; summaryId = null; continue;
    }
    if (event.kind !== 'agent') continue;
    seen.add(event.id);
    const { method, params = {} } = event.data;
    if (method === 'item/reasoning/summaryTextDelta') {
      const id = `${params.itemId}:${params.summaryIndex ?? 0}`;
      text = (summaryId === id ? text : '') + (params.delta || ''); summaryId = id;
    } else if (method === 'item/started') {
      const item = params.item || {};
      const activity = { commandExecution: 'Running a command', fileChange: 'Updating files',
        mcpToolCall: `Using ${item.tool || 'a tool'}`, webSearch: 'Searching the web',
        reasoning: 'Considering the next step', agentMessage: 'Writing a response' }[item.type];
      if (activity) { text = activity; summaryId = null; }
    } else if (method === 'item/completed' && params.item?.type === 'agentMessage' && params.item.phase === 'commentary') {
      text = params.item.text || text; summaryId = null;
    }
  }
  return text || 'Preparing the next activity…';
}

// Reconcile streamed model text with final items, preserving replay and turn boundaries.
export function taskThinking(events) {
  const blocks = new Map(), tools = new Set(), seen = new Set();
  let turnId = '';
  function block(params) {
    const id = `${params.turnId || turnId}:${params.item?.id || params.itemId}`;
    if (!blocks.has(id)) blocks.set(id, { id, content: [], summary: [], complete: false });
    return blocks.get(id);
  }
  const textParts = parts => parts.map(part => typeof part === 'string' ? part : part?.text || '');
  for (const event of events) {
    if (seen.has(event.id) || event.kind !== 'agent') continue;
    seen.add(event.id);
    const { method, params = {} } = event.data, item = params.item;
    if (method === 'turn/started') {
      for (const value of blocks.values()) value.complete = true;
      turnId = params.turn?.id || '';
    }
    if (method === 'turn/completed') for (const value of blocks.values()) value.complete = true;
    if (method === 'item/reasoning/textDelta' || method === 'item/reasoning/summaryTextDelta') {
      const value = block(params), raw = method === 'item/reasoning/textDelta';
      const parts = raw ? value.content : value.summary;
      const index = (raw ? params.contentIndex : params.summaryIndex) ?? 0;
      parts[index] = (parts[index] || '') + (params.delta || '');
    } else if (item?.type === 'reasoning' && method === 'item/completed') {
      const value = block(params);
      for (const key of ['content', 'summary']) {
        if (Array.isArray(item[key]) && item[key].length) value[key] = textParts(item[key]);
      }
      value.complete = true;
    } else if (method === 'item/started' && ['commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'imageView', 'dynamicToolCall'].includes(item?.type)) {
      tools.add(`${params.turnId || turnId}:${item.id}`);
    }
  }
  const steps = [...blocks.values()].map(value => ({ id: value.id, complete: value.complete,
    text: value.content.filter(Boolean).join('\n\n'), summary: value.summary.filter(Boolean).join('\n\n') }))
    .filter(value => value.text.trim() || value.summary.trim());
  // Prefer model-provided summaries. Otherwise show clearly labelled brief excerpts.
  const summaries = steps.flatMap(step => step.summary ? [step.summary] : []);
  const excerpts = steps.map(step => step.text.trim().split(/\n\s*\n/)[0]?.replace(/\s+/g, ' ') || '').filter(Boolean);
  const compact = text => text.length > 240 ? `${text.slice(0, 237).trimEnd()}…` : text;
  return { steps, liveText: steps.at(-1)?.text || steps.at(-1)?.summary || '',
    highlights: [...new Set(summaries.length ? summaries : excerpts)].slice(-3).map(compact),
    excerpts: !summaries.length, toolCount: tools.size };
}

export function taskTelemetry(task, events, now = Date.now()) {
  let usage = task?.tokenUsage || null, firstToken = null, started = null, compute = 0;
  const samples = [], activities = new Map();
  const providerTiming = events.some(event => event.kind === 'inference');
  const seen = new Set();
  for (const event of events) {
    if (seen.has(event.id)) continue;
    seen.add(event.id);
    const timestamp = event.createdAt;
    if (event.kind === 'inference') {
      if (event.data.phase === 'started') started ??= event.data.timestamp;
      else if (event.data.phase === 'output') {
        firstToken ??= event.data.timestamp;
        samples.push({ timestamp: event.data.timestamp, tokens: event.data.estimatedTokens });
      }
    }
    if (event.kind === 'usage') usage = event.data;
    if (event.kind !== 'agent' || !Number.isFinite(timestamp)) continue;
    const { method, params = {} } = event.data;
    if (method === 'turn/started' && !providerTiming) started ??= timestamp;
    if (!providerTiming && ['item/agentMessage/delta', 'item/reasoning/textDelta', 'item/reasoning/summaryTextDelta', 'item/dynamicToolCall/argumentsDelta'].includes(method) && params.delta) {
      firstToken ??= timestamp;
      samples.push({ timestamp, tokens: params.delta.length / 4 });
    }
    const item = params.item;
    const key = `${params.turnId || ''}:${item?.id}`;
    if (method === 'item/started' && ['commandExecution', 'dynamicToolCall', 'fileChange'].includes(item?.type)) {
      if (!activities.size) activities.set('_start', timestamp);
      activities.set(key, timestamp);
    } else if (method === 'item/completed' && activities.has(key)) {
      activities.delete(key);
      if (activities.size === 1) { compute += Math.max(0, timestamp - activities.get('_start')); activities.clear(); }
    }
  }
  const complete = terminalStatuses.has(task?.status);
  const end = complete ? task.completedAt ?? task.updatedAt : now / 1000;
  if (activities.size) compute += Math.max(0, end - activities.get('_start'));
  // Recent stream output, never total output divided by the whole task duration.
  // Native notifications expose text, not token IDs; label this rolling estimate.
  const rateEnd = complete ? samples.at(-1)?.timestamp ?? end : end;
  const windowStart = Math.max(started ?? task?.createdAt ?? rateEnd - 2, rateEnd - 2);
  const recent = samples.filter(sample => sample.timestamp > windowStart && sample.timestamp <= rateEnd);
  return { usage, estimated: true, tokensPerSecond: task?.status === 'paused' ? 0 : recent.reduce((sum, sample) => sum + sample.tokens, 0) / Math.max(.25, rateEnd - windowStart),
    ttft: firstToken == null ? null : Math.max(0, firstToken - (started ?? task.createdAt)), computeSeconds: compute };
}

// Keep first-seen positions while reconciling final text and duplicate SSE replay.
export function conversationTimeline(task, events, detail = 'standard') {
  const timeline = [], messages = new Map(), seen = new Set(), users = new Set();
  let turnId = '';
  const steering = events.filter(event => event.kind === 'steering');
  const nativeUsers = new Set(events.filter(event => event.kind === 'agent' && event.data.params?.item?.type === 'userMessage')
    .map(event => event.data.params.item.clientId || event.data.params.item.id));
  function user(id, text, attachments, messageId) {
    if (users.has(id)) return;
    users.add(id); timeline.push({ type: 'user', id, text, messageId: String(messageId), complete: true, ...(attachments?.length ? { attachments } : {}) });
  }
  for (const event of events) {
    if (seen.has(event.id)) continue;
    seen.add(event.id);
    if (event.kind === 'steering') {
      if (!nativeUsers.has(event.data.submissionId)) user(event.data.submissionId, event.data.prompt, event.data.attachments, event.id);
      continue;
    }
    if (event.kind !== 'agent') continue;
    const { method, params = {} } = event.data, item = params.item;
    if (method === 'turn/started') turnId = params.turn?.id || '';
    if (item?.type === 'userMessage') {
      // Original task prompts are rendered once by the task container.
      const clientId = item.clientId || item.id;
      const accepted = steering.find(value => value.data.submissionId === clientId);
      const text = accepted?.data.prompt || item.content?.filter(part => part.type === 'text').map(part => part.text).join('\n');
      if ((accepted || item.clientId) && text) user(clientId, text, accepted?.data.attachments, event.id);
      continue;
    }
    const isMessage = method === 'item/agentMessage/delta' || item?.type === 'agentMessage';
    if (isMessage) {
      const id = `${params.turnId || turnId}:${item?.id || params.itemId}`;
      let message = messages.get(id);
      if (!message) { message = { type: 'assistant', id, text: '', complete: false, messageId: String(event.id) }; messages.set(id, message); timeline.push(message); }
      if (method === 'item/agentMessage/delta' && !message.complete) message.text += params.delta || '';
      if (method === 'item/completed') Object.assign(message, { text: item.text || '', complete: true, phase: item.phase });
      continue;
    }
    if (detail === 'minimal') continue;
    const toolTypes = ['commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'imageView', 'dynamicToolCall'];
    if (detail !== 'full' && !toolTypes.includes(item?.type) && !/^item\/(commandExecution|fileChange|mcpToolCall|dynamicToolCall)\//.test(method)) continue;
    let block = timeline.at(-1);
    if (block?.type !== 'details') { block = { type: 'details', id: event.id, events: [] }; timeline.push(block); }
    block.events.push(event);
  }
  return timeline.filter(item => item.type !== 'assistant' || item.text.trim());
}

export function executionItems(events) {
  const items = new Map();
  for (const event of events) {
    const { method, params = {} } = event.data;
    const id = `${params.turnId || ''}:${params.item?.id || params.itemId || event.id}`;
    const previous = items.get(id) || { id, type: 'activity', output: '' };
    if (params.item) items.set(id, { ...previous, ...params.item });
    else if (params.delta) items.set(id, { ...previous, output: (previous.aggregatedOutput ?? previous.output) + params.delta, aggregatedOutput: undefined });
    else items.set(id, { ...previous, label: method });
  }
  return [...items.values()];
}
