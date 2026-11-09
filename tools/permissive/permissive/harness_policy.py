"""Current capabilities and bounded, content-free tool-repair context."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re

MAX_CONTRACT_FAILURES = 3
CAPABILITY_TAG = 'permissive_capabilities'
REPAIR_TAG = 'permissive_tool_repair'
NATIVE_TOOLS = {'exec', 'wait', 'exec_command', 'write_stdin', 'apply_patch'}


class ToolContractFailure(ValueError):
    pass


def desktop_capability():
    agent = Path(os.environ.get('PERMISSIVE_DESKTOP_AGENT', str(Path(__file__).resolve().parents[2]/'native/desktop-agent.exe')))
    if importlib.util.find_spec('permissive.desktop_worker') is None:
        return {'status': 'unavailable', 'reason': 'desktop_worker module is missing from this installation'}
    if not agent.is_file():
        return {'status': 'unavailable', 'reason': 'desktop agent executable is missing from this installation'}
    return {'status': 'available'}


def capabilities(request, browser_probe=None):
    browser = browser_probe(request) if browser_probe else {
        'status': 'launch_on_demand' if request.get('browserMode') == 'dedicated' else 'unknown',
        'reason': 'Browser connection has not been checked by this execution service.'}
    return {'shell': {'status': 'available', 'dialect': 'PowerShell' if os.name == 'nt' else 'POSIX'},
            'http': {'status': 'available', 'defaultTimeoutSeconds': 30},
            'browser': browser, 'desktop': desktop_capability()}


def output_text(value):
    if isinstance(value, str):
        return value[:8192]
    if isinstance(value, list):
        return ''.join(part.get('text', '') for part in value
                       if isinstance(part, dict) and part.get('type') in {'input_text', 'text'})[:8192]
    return ''


def contract_error(name, text):
    if name not in NATIVE_TOOLS:
        return None
    # Framework parse errors have a precise prefix. Never classify an arbitrary
    # browser, document, command-output substring or quoted task text as a failure.
    match = re.match(r'^failed to parse function arguments: missing field `([A-Za-z_][A-Za-z0-9_]*)`', text)
    if match:
        return 'missing required field ' + match[1]
    if text.startswith(('unrecognized function name:', 'Unknown tool:')):
        return 'tool name is not advertised'
    if name in {'exec', 'wait'} and text.startswith('Script failed\n'):
        payload = text.partition('Output:\n')[2].lstrip()
        if re.match(r'^(?:ReferenceError|SyntaxError|TypeError):', payload):
            return 'JavaScript tool invocation failed'
    return None


def recovery_hint(items):
    calls, outputs = {}, []
    for item in items[-128:]:
        kind = item.get('type')
        if kind in {'function_call', 'custom_tool_call'}:
            calls[item.get('call_id')] = item
        elif kind in {'function_call_output', 'custom_tool_call_output'}:
            call = calls.get(item.get('call_id'))
            if call:
                outputs.append((call, output_text(item.get('output'))))
    failures = []
    for call, text in reversed(outputs):
        error = contract_error(call.get('name'), text)
        if error is None:
            break
        failures.append(error)
    if len(failures) >= MAX_CONTRACT_FAILURES:
        raise ToolContractFailure('Repeated invalid tool calls without a successful operation. '
                                  'No arguments were guessed or commands executed for the rejected calls. '
                                  'Retry from saved progress with the advertised code-mode tool contract.')
    if failures:
        return ('A tool call was rejected before execution: ' + failures[0] + '. '
                'Use the advertised JavaScript exec tool. Discover a declaration with '
                'text(ALL_TOOLS.filter(t => /http|browser/.test(t.name))); '
                'Execute PowerShell with text(await tools.exec_command({cmd:"Get-Date"})); '
                'Pass an object containing cmd to exec_command. JavaScript belongs in exec, not in cmd. '
                'Correct the next call; do not use empty objects or substitute echo/true checks. '
                f'{len(failures)} consecutive contract failure(s); the harness permits two correction attempts.')
    # A silent terminal is a different phase from inference. Advise inspection,
    # not a blanket command deadline that could interrupt legitimate long work.
    polls, session = 0, None
    for call, text in reversed(outputs):
        if call.get('name') != 'write_stdin':
            break
        try:
            arguments = json.loads(call.get('arguments', '{}'))
        except (ValueError, TypeError):
            break
        current = arguments.get('session_id')
        if arguments.get('chars', '') or current is None or (session is not None and current != session):
            break
        if 'Process running with session ID' not in text or text.partition('Output:')[2].strip():
            break
        session = current;polls += 1
    if polls >= 3:
        return ('The same terminal session has returned no new output across repeated polls. '
                'Inspect its task-owned process or interrupt that execution session with Ctrl+C before replanning. '
                'Do not terminate shared services or processes by executable name. '
                'Use the HTTP tool with an explicit request deadline for network calls. '
                'A legitimate long operation may continue when you verify that it is making progress.')
    return None


def harness_request(body, current):
    result = copy.deepcopy(body)
    items = result.get('input', [])
    if not isinstance(items, list):
        raise ValueError('Harness context requires native message history.')
    hint = recovery_hint(items)
    patterns = [re.compile(r'<'+tag+r'>.*?</'+tag+r'>', re.DOTALL) for tag in (CAPABILITY_TAG, REPAIR_TAG)]
    for item in items:
        if item.get('role') != 'developer':
            continue
        content = item.get('content')
        if isinstance(content, str):
            for pattern in patterns:
                content = pattern.sub('', content)
            item['content'] = content
        elif isinstance(content, list):
            for part in content:
                if part.get('type') == 'input_text':
                    for pattern in patterns:
                        part['text'] = pattern.sub('', part.get('text', ''))
    text = '<'+CAPABILITY_TAG+'>\n'+json.dumps(current, separators=(',', ':'))+'\n'
    text += ('Use the capabilities reported here. A disconnected attached browser needs its selected extension connected in chat options; '
             'available HTTP tools can still perform an API task. HTTP access does not establish visible-browser access. '
             'A missing helper is an installation problem; report it rather than attempting to modify shared application infrastructure.\n')
    text += '</'+CAPABILITY_TAG+'>'
    if hint:
        text += '\n<'+REPAIR_TAG+'>\n'+hint+'\n</'+REPAIR_TAG+'>'
    items.append({'role': 'developer', 'content': [{'type': 'input_text', 'text': text}]})
    return result
