import test from 'node:test';
import assert from 'node:assert/strict';
import { activitySentence, conversationAttachments, statusLabel, assistantMessages, elapsedSeconds, taskProgress, taskRecovery, taskThinking, taskTelemetry, conversationTimeline, executionItems } from '../desktop/conversation.mjs';
import { tableWorkbook } from '../desktop/table-export.mjs';
import { unzipSync, strFromU8 } from 'fflate';

test('recovery status shows bounded progress and ignores replayed or unknown events', () => {
  const retrying = { id: 1, kind: 'recovery', data: { phase: 'retrying', attempt: 1, maxAttempts: 2, message: 'Reconnecting to the same conversation.' } };
  assert.deepEqual(taskRecovery([retrying]), { phase: 'retrying', title: 'Retrying response · 1 of 2', message: 'Reconnecting to the same conversation.' });
  assert.equal(taskRecovery([{ id: 2, kind: 'agent', data: { phase: 'retrying' } }]), null);
  assert.equal(taskRecovery([retrying, { id: 3, kind: 'recovery', data: { phase: 'recovered' } }, retrying]), null);
  assert.equal(taskRecovery([{ id: 4, kind: 'recovery', data: { phase: 'requested' } }]).title, 'Response retry requested');
  assert.equal(taskRecovery([{ id: 5, kind: 'recovery', data: { phase: 'exhausted' } }, { id: 6, kind: 'recovery', data: { phase: 'other' } }]).title, 'Automatic response retries exhausted');
});

test('history restoration gives progress until native activity begins', () => {
  const restoring = { id: 1, kind: 'recovery', data: { phase: 'history', message: 'Restoring the retained conversation.' } };
  const ready = { id: 2, kind: 'recovery', data: { phase: 'history_ready', message: 'The retained conversation is ready.' } };
  assert.equal(taskRecovery([restoring]), null);
  assert.equal(taskProgress([restoring]), 'Preparing response…');
  assert.equal(taskRecovery([restoring, ready]), null);
  assert.equal(taskProgress([restoring, ready, {id:3,kind:'agent',data:{method:'item/started',params:{item:{type:'reasoning'}}}}]), 'Considering the next step');
});

test('retry notices clear after activity or success, while real failure stays actionable', () => {
  const retry = { id: 1, kind: 'recovery', data: { phase: 'retrying' } };
  assert.equal(taskRecovery([retry], 'completed'), null);
  assert.equal(taskRecovery([retry, { id: 2, kind: 'agent', data: { method: 'item/agentMessage/delta' } }], 'running'), null);
  const exhausted = { id: 3, kind: 'recovery', data: { phase: 'exhausted' } };
  assert.equal(taskRecovery([retry, exhausted], 'completed'), null);
  assert.equal(taskRecovery([retry, exhausted], 'failed').phase, 'exhausted');
});

test('timeline interleaves details and reconciles streamed messages without replay duplicates', () => {
  const agent = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [agent(1, 'item/agentMessage/delta', { itemId: 'a', delta: 'Checking' }),
    agent(2, 'item/started', { item: { id: 'cmd', type: 'commandExecution', command: 'echo example', aggregatedOutput: '' } }),
    agent(3, 'item/commandExecution/outputDelta', { itemId: 'cmd', delta: 'example' }),
    agent(4, 'item/completed', { item: { id: 'cmd', type: 'commandExecution', aggregatedOutput: 'example', status: 'completed' } }),
    agent(5, 'item/completed', { item: { id: 'a', type: 'agentMessage', text: 'Checking now.' } }),
    agent(6, 'item/completed', { item: { id: 'b', type: 'agentMessage', text: 'Done' } })];
  const timeline = conversationTimeline({}, [...events, events[2]]);
  assert.deepEqual(timeline.map(item => item.type), ['assistant', 'details', 'assistant']);
  assert.equal(timeline[0].text, 'Checking now.');
  assert.equal(executionItems(timeline[1].events).length, 1);
  assert.equal(executionItems(timeline[1].events)[0].aggregatedOutput, 'example');
  assert.equal(executionItems(timeline[1].events.slice(0, 2))[0].output, 'example');
  assert.deepEqual(conversationTimeline({}, events, 'minimal').map(item => item.type), ['assistant', 'assistant']);
});

test('steering native user echoes and accepted records display exactly once in their original position', () => {
  const event = (id, item) => ({ id, kind: 'agent', data: { method: 'item/completed', params: { item } } });
  const events = [event(1, { id: 'original', type: 'userMessage' }),
    event(2, { id: 'native-item-id', clientId: 'steering-id', type: 'userMessage' }),
    event(3, { id: 'response', type: 'agentMessage', text: 'Following your direction' }),
    { id: 4, kind: 'steering', data: { submissionId: 'steering-id', prompt: 'Change direction' } }];
  const timeline = conversationTimeline({}, events);
  assert.deepEqual(timeline.map(item => item.type), ['user', 'assistant']);
  assert.equal(timeline[0].text, 'Change direction');
  assert.equal(conversationTimeline({}, [events[3], events[3]]).length, 1);
  const unconfirmed = event(5, { id: 'native-user', clientId: 'pending-id', type: 'userMessage', content: [{ type: 'text', text: 'Pending steering' }] });
  assert.equal(conversationTimeline({}, [unconfirmed])[0].text, 'Pending steering');
});

test('automatic native turns do not merge reused provider message IDs', () => {
  const event = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [event(1, 'turn/started', { turn: { id: 'turn-1' } }),
    event(2, 'item/completed', { item: { id: 'answer', type: 'agentMessage', text: 'First turn' } }),
    event(3, 'turn/started', { turn: { id: 'turn-2' } }),
    event(4, 'item/completed', { item: { id: 'answer', type: 'agentMessage', text: 'Second turn' } })];
  assert.deepEqual(conversationTimeline({}, events).filter(item => item.type === 'assistant').map(item => item.text), ['First turn', 'Second turn']);
});

test('chat merges streamed assistant text, reconciles completion and excludes tool/reasoning output', () => {
  const event = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [event(1, 'item/agentMessage/delta', { itemId: 'a', delta: 'Hello ' }),
    event(2, 'item/agentMessage/delta', { itemId: 'a', delta: 'world' }),
    event(2, 'item/agentMessage/delta', { itemId: 'a', delta: 'world' }),
    event(3, 'item/commandExecution/outputDelta', { delta: 'private tool output' }),
    event(4, 'item/reasoning/textDelta', { delta: 'reasoning' }),
    event(5, 'item/completed', { item: { id: 'a', type: 'agentMessage', text: 'Hello world!', phase: 'final_answer' } })];
  assert.deepEqual(assistantMessages(events), [{ id: 'a', text: 'Hello world!', complete: true, phase: 'final_answer' }]);
});

test('terminal timers stay frozen across ticks and reloads, including older task snapshots', () => {
  for (const status of ['completed', 'failed', 'cancelled', 'interrupted']) {
    const task = { createdAt: 100, completedAt: 160, updatedAt: 200, status };
    assert.equal(elapsedSeconds(task, 1000000), 60);
    assert.equal(elapsedSeconds(task, 2000000), 60);
    assert.equal(elapsedSeconds({ ...task, completedAt: null }, 2000000), 100);
  }
  assert.equal(elapsedSeconds({ status: 'running', createdAt: 100 }, 170000), 70);
  assert.equal(elapsedSeconds({ status: 'paused', createdAt: 100, updatedAt: 150 }, 170000), 50);
});

test('visible progress uses available summaries, commentary and tool activity', () => {
  const agent = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [agent(1, 'item/reasoning/textDelta', { delta: 'PRIVATE' }),
    agent(2, 'item/reasoning/summaryTextDelta', { itemId: 'r', delta: 'Checking ' }),
    agent(3, 'item/reasoning/summaryTextDelta', { itemId: 'r', delta: 'the data.' })];
  assert.equal(taskProgress([...events, events[2]]), 'Checking the data.');
  assert.equal(taskProgress([...events, agent(4, 'item/started', { item: { type: 'commandExecution' } })]), 'Running a command');
  assert.equal(taskProgress([agent(5, 'item/completed', { item: { type: 'agentMessage', phase: 'commentary', text: 'Found two issues.' } })]), 'Found two issues.');
});

test('thinking streams raw text, reconciles final content, and isolates reused IDs across turns', () => {
  const agent = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [agent(1, 'turn/started', { turn: { id: 'first' } }),
    agent(2, 'item/reasoning/textDelta', { itemId: 'r', contentIndex: 0, delta: 'Inspecting ' }),
    agent(3, 'item/reasoning/textDelta', { itemId: 'r', contentIndex: 0, delta: 'the files.' })];
  assert.equal(taskThinking([...events, events[2]]).liveText, 'Inspecting the files.');
  events.push(agent(4, 'item/started', { item: { id: 'tool', type: 'dynamicToolCall' } }));
  assert.equal(taskThinking(events).liveText, 'Inspecting the files.');
  events.push(agent(5, 'item/completed', { item: { id: 'r', type: 'reasoning', content: ['Inspected the files.', 'Checking the results.'], summary: [] } }),
    agent(6, 'turn/started', { turn: { id: 'second' } }),
    agent(7, 'item/reasoning/textDelta', { itemId: 'r', delta: 'Verifying the repair.' }),
    agent(8, 'item/started', { item: { id: 'tool', type: 'dynamicToolCall' } }));
  const thinking = taskThinking([...events, events[3]]);
  assert.deepEqual(thinking.steps.map(step => step.text), ['Inspected the files.\n\nChecking the results.', 'Verifying the repair.']);
  assert.equal(thinking.liveText, 'Verifying the repair.');
  assert.equal(thinking.toolCount, 2);
  assert.equal(thinking.excerpts, true);
  assert.deepEqual(thinking.highlights, ['Inspected the files.', 'Verifying the repair.']);
});

test('thinking summaries prefer supplied summaries and keep long history behind bounded excerpts', () => {
  const agent = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [agent(1, 'item/reasoning/textDelta', { itemId: 'r', delta: 'x'.repeat(5000) }),
    agent(2, 'item/completed', { item: { id: 'r', type: 'reasoning', content: [], summary: [] } })];
  assert.equal(taskThinking(events).steps[0].text.length, 5000);
  assert.equal(taskThinking(events).highlights[0].length, 238);
  events.push(agent(3, 'item/reasoning/summaryTextDelta', { itemId: 'r', summaryIndex: 0, delta: 'Verified the ' }),
    agent(4, 'item/reasoning/summaryTextDelta', { itemId: 'r', summaryIndex: 0, delta: 'repair.' }));
  assert.equal(taskThinking(events).excerpts, false);
  assert.deepEqual(taskThinking(events).highlights, ['Verified the repair.']);
  assert.equal(taskThinking(events).liveText.length, 5000);
});

test('rolling rate decays to zero while waiting; TTFT and compute use event timestamps', () => {
  const task = { status: 'running', createdAt: 100 };
  const usage = { inputTokens: 100, cachedInputTokens: 20, outputTokens: 10, reasoningOutputTokens: 4, totalTokens: 110 };
  const delta = { id: 1, createdAt: 109, kind: 'agent', data: { method: 'item/agentMessage/delta', params: { delta: 'a'.repeat(40) } } };
  assert.equal(taskTelemetry(task, [delta, delta], 110000).tokensPerSecond, 5);
  const events = [delta, { id: 2, kind: 'usage', data: usage }];
  assert.equal(taskTelemetry(task, events, 110000).estimated, true);
  assert.equal(taskTelemetry(task, events, 110000).tokensPerSecond, 5);
  assert.equal(taskTelemetry(task, events, 112000).tokensPerSecond, 0);
  assert.equal(taskTelemetry(task, events, 110000).ttft, 9);
  assert.deepEqual(taskTelemetry({ ...task, status: 'completed', completedAt: 110, tokenUsage: usage }, [], 200000).usage, usage);
});

test('provider timing includes hidden tool output and ignores native duplicates; compute intervals overlap once', () => {
  const task = { status: 'running', createdAt: 100 };
  const agent = (id, at, method, item) => ({ id, createdAt: at, kind: 'agent', data: { method, params: { turnId: 't', item } } });
  const events = [
    { id: 1, kind: 'inference', data: { phase: 'started', timestamp: 102.25 } },
    { id: 2, kind: 'inference', data: { phase: 'output', timestamp: 102.75, estimatedTokens: 4 } },
    { id: 3, kind: 'agent', createdAt: 102.8, data: { method: 'item/agentMessage/delta', params: { delta: 'x'.repeat(16) } } },
    agent(4, 103, 'item/started', { id: 'a', type: 'dynamicToolCall' }),
    agent(5, 104, 'item/started', { id: 'b', type: 'commandExecution' }),
    agent(6, 106, 'item/completed', { id: 'b', type: 'commandExecution' }),
    agent(7, 107, 'item/completed', { id: 'a', type: 'dynamicToolCall' }),
  ];
  const live = taskTelemetry(task, events, 103250);
  assert.equal(live.ttft, .5);
  assert.equal(live.tokensPerSecond, 4);
  assert.equal(taskTelemetry(task, events.slice(0, -1), 108000).computeSeconds, 5);
  const completed = { ...task, status: 'completed', completedAt: 109 };
  assert.equal(taskTelemetry(completed, [...events, events[6]], 999000).computeSeconds, 4);
  assert.deepEqual(taskTelemetry(completed, events, 999000), taskTelemetry(completed, events, 1000000));
});

test('Excel table export preserves IDs, escapes XML, stores formulas as text and aligns cells', () => {
  const bytes = tableWorkbook({ headers: ['Name', 'Value'], alignments: ['left', 'right'], rows: [['A&B', '42'], ['001', '=SUM(A1)']] });
  const files = unzipSync(bytes);
  const sheet = strFromU8(files['xl/worksheets/sheet1.xml']);
  assert.match(sheet, /A&amp;B/);
  assert.match(sheet, /<c r="B2" s="2"><v>42<\/v>/);
  assert.match(sheet, /<c r="A3" s="0" t="inlineStr"/);
  assert.match(sheet, /=SUM\(A1\)/);
  assert.doesNotMatch(sheet, /<f>/);
  assert.match(sheet, /autoFilter ref="A1:B3"/);
});

test('thinking steps finish independently while the task keeps streaming the next step', () => {
  const agent = (id, method, params) => ({ id, kind: 'agent', data: { method, params } });
  const events = [agent(1, 'item/reasoning/textDelta', { itemId: 'a', delta: 'Inspecting files.' }),
    agent(2, 'item/completed', { item: { id: 'a', type: 'reasoning', summary: ['Inspected the files. Found two issues.'] } }),
    agent(3, 'item/reasoning/textDelta', { itemId: 'b', delta: 'Checking the repair.' })];
  assert.deepEqual(taskThinking(events).steps.map(step => step.complete), [true, false]);
  assert.equal(activitySentence(taskThinking(events).steps[0].summary), 'Inspected the files.');
  assert.equal(taskThinking([...events, agent(4, 'turn/completed', { turn: { id: 'turn' } })]).steps[1].complete, true);
  assert.equal(activitySentence('**Checking files**\n\nMore information.'), 'Checking files More information.');
  assert.equal(activitySentence(''), 'Completed a thinking step.');
  assert.ok(activitySentence('x'.repeat(500)).length <= 220);
  assert.equal(activitySentence('Ran `check_files.py *.jpg`. More details.'), 'Ran check_files.py *.jpg.');
  assert.equal(activitySentence('Ran check_files.py **/*.jpg **/*.png'), 'Ran check_files.py **/*.jpg **/*.png.');
});

test('attachment tray counts all task artifacts and deduplicates input and replay attachments', () => {
  const tasks = [{ id: 'a', request: { attachments: ['input', { id: 'photo', name: 'photo.jpg' }] }, artifacts: [{ id: 'file', name: 'report.zip' }] },
    { id: 'b', request: { attachments: ['input'] }, artifacts: [{ id: 'file', name: 'second.png' }] }];
  const steer = { kind: 'steering', taskId: 'b', data: { attachments: ['photo', 'new'] } };
  const files = conversationAttachments(tasks, [steer, steer]);
  assert.equal(files.length, 5);
  assert.equal(files.filter(file => file.input).length, 3);
  assert.deepEqual(files.filter(file => !file.input).map(file => file.taskId), ['a', 'b']);
  assert.equal(statusLabel('running'), 'Running'); assert.equal(statusLabel('completed'), 'Done');
});
