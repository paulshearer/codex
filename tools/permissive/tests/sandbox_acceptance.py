"""Live DeepSeek/desktop acceptance through a bound, signed Hyper-V controller.

This script intentionally submits a real model task unless --prepare-only is
used. It never starts a native agent or drives a desktop on the host. A fixed,
read-only PowerShell Direct probe independently checks the guest hostname and
the completed native thread's model metadata. Run from an elevated host shell
after provisioning and package verification.
"""

import argparse
import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import struct
import tempfile
import time
import uuid

from aiohttp import ClientTimeout

from permissive.client import SignedSession
from permissive.vm_runtime import deployment


MODEL = '/models/deepseek-v4-flash'
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
REQUIRED_OPERATIONS = {'status', 'windows', 'launch', 'inspect', 'action', 'capture'}

# All executable code is fixed. BindingPath and ThreadId are data parameters;
# neither prompts nor model-generated commands enter this host-side script.
PROBE = r'''param([Parameter(Mandatory)][string]$BindingPath,[string]$ThreadId)
$ErrorActionPreference = 'Stop'
$binding = Get-Content -LiteralPath $BindingPath -Raw | ConvertFrom-Json
if ($binding.role -ne 'controller') { throw 'An explicit controller binding is required.' }
[Guid]::Parse($binding.vmId) | Out-Null
if ($ThreadId) { [Guid]::Parse($ThreadId) | Out-Null }
. (Join-Path $binding.packagePath 'windows/sandbox/common.ps1')
Assert-SandboxAdministrator
$binding = Read-SandboxBinding $BindingPath
Test-SandboxPerimeter $binding | Out-Null
$credential = Get-SandboxCredential $binding
$result = Invoke-Command -VMId ([Guid]$binding.vmId) -Credential $credential -ScriptBlock {
    param([string]$NativeThreadId)
    $ErrorActionPreference = 'Stop'
    $contexts = @(); $sessions = @(); $files = @(); $readBytes = 0L
    $budgetExceeded = $false; $fileLimitExceeded = $false; $readBudget = 64MB
    if ($NativeThreadId) {
        [Guid]::Parse($NativeThreadId) | Out-Null
        $root = 'C:\ProgramData\CodexPermissiveGuest\native-home\sessions'
        $candidates = @(Get-ChildItem -LiteralPath $root -Recurse -File -Filter ('*' + $NativeThreadId + '.jsonl') | Select-Object -First 9)
        $fileLimitExceeded = $candidates.Count -gt 8
        foreach ($file in $candidates | Select-Object -First 8) {
            $remaining = $readBudget - $readBytes
            if ($remaining -le 0) { $budgetExceeded = $true; break }
            $limit = [int][Math]::Min([long]$file.Length, $remaining)
            if ($file.Length -gt $remaining) { $budgetExceeded = $true }
            $buffer = New-Object byte[] $limit
            $count = 0
            $stream = [IO.File]::Open($file.FullName,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
            try {
                while ($count -lt $limit) {
                    $part = $stream.Read($buffer,$count,$limit-$count)
                    if ($part -eq 0) { break }
                    $count += $part
                }
            } finally { $stream.Dispose() }
            $readBytes += $count
            $matched = $false
            foreach ($line in ([Text.Encoding]::UTF8.GetString($buffer,0,$count) -split '\r?\n')) {
                try { $entry = $line | ConvertFrom-Json } catch { continue }
                if ($entry.type -eq 'session_meta' -and $entry.payload.id -eq $NativeThreadId) {
                    $matched = $true
                    if ($sessions.Count -lt 8) {
                        $provider = [string]$entry.payload.model_provider
                        $sessions += [pscustomobject]@{id=$NativeThreadId;modelProvider=$provider.Substring(0,[Math]::Min($provider.Length,512))}
                    }
                }
                if ($matched -and $entry.type -eq 'turn_context') {
                    $model = [string]$entry.payload.model; $provider = [string]$entry.payload.model_provider; $stamp = [string]$entry.timestamp
                    $context = [pscustomobject]@{model=$model.Substring(0,[Math]::Min($model.Length,512));
                        modelProvider=$provider.Substring(0,[Math]::Min($provider.Length,512));timestamp=$stamp.Substring(0,[Math]::Min($stamp.Length,128))}
                    $contexts = @(@($contexts) + $context | Select-Object -Last 16)
                }
            }
            if ($matched) {
                $hash = [Security.Cryptography.SHA256]::Create()
                try { $digest = [BitConverter]::ToString($hash.ComputeHash($buffer,0,$count)).Replace('-','').ToLowerInvariant() }
                finally { $hash.Dispose() }
                $files += [pscustomobject]@{name=$file.Name;bytes=$file.Length;bytesRead=$count;sha256=$digest;hashScope=$(if ($file.Length -eq $count) {'complete'} else {'read-prefix'})}
            }
            [Array]::Clear($buffer,0,$buffer.Length)
        }
    }
    $os = Get-CimInstance Win32_OperatingSystem
    [pscustomobject]@{hostname=[Environment]::MachineName;osVersion=$os.Version;osBuild=$os.BuildNumber;osCaption=$os.Caption;osArchitecture=$os.OSArchitecture;
        nativeThreadId=$NativeThreadId;sessions=@($sessions);contexts=@($contexts);rollouts=@($files);
        readBytes=$readBytes;readBudgetExceeded=$budgetExceeded;fileLimitExceeded=$fileLimitExceeded}
} -ArgumentList $ThreadId
$result | Select-Object hostname,osVersion,osBuild,osCaption,osArchitecture,nativeThreadId,sessions,contexts,rollouts,readBytes,readBudgetExceeded,fileLimitExceeded | ConvertTo-Json -Depth 8 -Compress
'''


class AcceptanceFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def uuid_text(value):
    return str(uuid.UUID(str(value)))


def verify_binding(binding):
    require(binding.get('role') == 'controller', 'Refusing an unbound/local runtime: deployment role must be controller.')
    require(binding.get('schema') == 'codex-permissive-sandbox-binding/v1', 'Expected a provisioned Hyper-V sandbox binding.')
    for name in ('vmId', 'sandboxId'):
        uuid_text(binding.get(name))
    require(binding.get('guestCredentialPath') and binding.get('packagePath'), 'Controller binding lacks its credential/package identity.')


def verify_controller(health, environment, binding):
    require(health.get('executionRole') == 'controller', 'The endpoint is not a host controller; no task was submitted.')
    require(uuid_text(environment.get('vmId')) == uuid_text(binding['vmId']), 'Controller environment VM ID does not match the explicit binding.')
    require(uuid_text(environment.get('environmentId')) == uuid_text(binding['sandboxId']), 'Controller environment ID does not match the explicit binding.')
    require(environment.get('phase') != 'exported', 'The controller is fenced for export.')
    if health.get('vmId'):
        require(uuid_text(health['vmId']) == uuid_text(binding['vmId']), 'Health response identifies another VM.')
    if health.get('environmentId'):
        require(uuid_text(health['environmentId']) == uuid_text(binding['sandboxId']), 'Health response identifies another environment.')
    require(health.get('maxConcurrentModelRequests') == 1, 'Acceptance requires one active task per sandbox VM.')


def verify_execution(task, binding):
    execution = task.get('execution') or {}
    require(execution.get('location') == 'hyperv', 'Task execution is not recorded as Hyper-V; refusing local fallback.')
    require(uuid_text(execution.get('vmId')) == uuid_text(binding['vmId']), 'Task executed in another VM.')
    require(uuid_text(execution.get('environmentId')) == uuid_text(binding['sandboxId']), 'Task executed in another environment.')
    require(execution.get('attemptId') and type(execution.get('generation')) is int, 'Missing durable VM attempt/generation receipt.')
    return execution


def tool_result(item):
    result = item.get('result') or {}
    for part in result.get('content', []):
        if part.get('type') == 'text':
            try:
                value = json.loads(part.get('text', ''))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and 'ok' in value:
                return value
    return result.get('structuredContent') or {}


def desktop_evidence(events):
    items = [event['data'].get('params', {}).get('item', {}) for event in events
             if event.get('kind') == 'agent' and event.get('data', {}).get('method') == 'item/completed']
    desktop = [item for item in items if item.get('type') == 'mcpToolCall' and item.get('server') == 'desktop' and item.get('tool') == 'desktop']
    successful = [item for item in desktop if item.get('status') == 'completed' and not item.get('error') and tool_result(item).get('ok')]
    operations = {item.get('arguments', {}).get('operation') for item in successful}
    require(REQUIRED_OPERATIONS <= operations, 'Missing successful native desktop operations: ' + ', '.join(sorted(REQUIRED_OPERATIONS - operations)))
    statuses = [tool_result(item) for item in successful if item.get('arguments', {}).get('operation') == 'status']
    require(any(value.get('session', {}).get('canInject') is True and value['session'].get('locked') is False
                and value['session'].get('sessionId', 0) > 0 and value['session'].get('integrity', -1) >= 0x3000 for value in statuses),
            'Desktop status did not establish an elevated unlocked interactive guest session.')
    displays = []
    for item in successful:
        if item.get('arguments', {}).get('operation') != 'inspect':
            continue
        result = tool_result(item)
        if 'calculator' not in str(result.get('window', {}).get('title', '')).lower():
            continue
        for element in result.get('elements', []):
            for field in ('name', 'value', 'text'):
                text = str(element.get(field, '')).strip()
                if text == '703' or re.search(r'(?:display|result|answer)\D*703(?:\D|$)', text, re.IGNORECASE):
                    displays.append({'field': field, 'text': text, 'window': result.get('window'), 'callId': item.get('id')})
    require(displays, 'No actual Calculator UIA result exposed 703; assistant claims alone are insufficient.')
    captures = [item for item in successful if item.get('arguments', {}).get('operation') == 'capture']
    require(any(any(part.get('type') == 'image' for part in (item.get('result') or {}).get('content', [])) for item in captures),
            'No native capture returned a model-vision image block.')
    commands = [item for item in items if item.get('type') == 'commandExecution' and item.get('status') == 'completed' and item.get('exitCode') == 0]
    require(commands, 'No successful guest native command exported the Unicode/file evidence.')
    summaries = []
    for item in desktop:
        value = tool_result(item)
        summaries.append({'id': item.get('id'), 'status': item.get('status'), 'arguments': item.get('arguments'),
                          'ok': value.get('ok'), 'method': value.get('method'), 'outcome': value.get('outcome'), 'error': value.get('error')})
    return {'operations': sorted(operations), 'desktopCalls': summaries, 'calculatorDisplays': displays,
            'commandExecutions': len(commands), 'captureCalls': len(captures), 'sessionStatuses': statuses}


def verify_probe(probe, binding, native_thread, expected_model):
    require(probe.get('hostname') and probe['hostname'].lower() != socket.gethostname().lower(), 'Independent guest hostname matches host or is unavailable.')
    require(probe.get('nativeThreadId') == native_thread, 'Native model probe inspected another thread.')
    require(probe.get('rollouts') and probe.get('sessions') and probe.get('contexts'), 'No independent native rollout model evidence was found.')
    require(not probe.get('readBudgetExceeded') and not probe.get('fileLimitExceeded'), 'Native model probe exceeded its bounded evidence-read budget.')
    providers = {session.get('modelProvider') for session in probe['sessions'] if session.get('id') == native_thread}
    providers |= {context.get('modelProvider') for context in probe['contexts'] if context.get('modelProvider')}
    require(providers and providers == {'deepseek'}, 'Native rollout provider is not exclusively DeepSeek: ' + repr(sorted(providers)))
    models = {context.get('model') for context in probe['contexts'] if context.get('model')}
    allowed = {expected_model, 'deepseek::flash'} if expected_model == MODEL else {expected_model}
    require(models and models <= allowed, 'Unexpected native model metadata: ' + repr(sorted(models)))
    require(int(probe.get('osBuild', 0)) >= 22000 and 'windows 11 pro' in str(probe.get('osCaption', '')).lower()
            and probe.get('osArchitecture') == '64-bit', 'Independent guest OS is not Windows 11 Pro x64.')
    return {'hostname': probe['hostname'], 'osVersion': probe.get('osVersion'), 'osBuild': probe.get('osBuild'),
            'osCaption': probe.get('osCaption'), 'osArchitecture': probe.get('osArchitecture'),
            'providers': sorted(providers), 'models': sorted(models), 'rollouts': probe['rollouts']}


async def fixed_probe(binding_path, thread_id, directory, timeout):
    if thread_id:
        uuid_text(thread_id)
    with tempfile.TemporaryDirectory(prefix='.sandbox-probe-', dir=directory) as temporary:
        script = Path(temporary) / 'probe.ps1'
        script.write_text(PROBE, encoding='utf-8-sig')
        arguments = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script), '-BindingPath', str(binding_path)]
        if thread_id:
            arguments.extend(['-ThreadId', thread_id])
        process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                     creationflags=0x08000000 if os.name == 'nt' else 0)
        async def bounded_output(stream):
            chunks, size = [], 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return b''.join(chunks)
                size += len(chunk)
                require(size <= 2 * 1024 * 1024, 'Fixed guest evidence probe exceeded its 2 MiB output budget.')
                chunks.append(chunk)
        readers = [asyncio.create_task(bounded_output(process.stdout)), asyncio.create_task(bounded_output(process.stderr)), asyncio.create_task(process.wait())]
        try:
            stdout, stderr, _ = await asyncio.wait_for(asyncio.gather(*readers), timeout)
        except BaseException:
            for reader in readers:
                reader.cancel()
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        require(process.returncode == 0, 'Fixed guest evidence probe failed: ' + stderr.decode('utf-8', 'replace')[-1200:])
        try:
            return json.loads(stdout.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as error:
            raise AcceptanceFailure('Fixed guest evidence probe returned invalid JSON.') from error


async def events_page(client, url, after, seconds=1, limit=200):
    """Read bounded complete SSE records; an unfinished record is re-read later."""
    rows, fields, more = [], [], False
    try:
        async with asyncio.timeout(seconds):
            async with client.get(url + '?after=' + str(after)) as response:
                require(response.status == 200, 'Controller event stream is unavailable.')
                while True:
                    line = await response.content.readline()
                    if not line:
                        break
                    if line.startswith(b'data:'):
                        fields.append(line[5:].strip())
                    elif not line.strip() and fields:
                        value = json.loads(b'\n'.join(fields))
                        fields = []
                        require(type(value.get('id')) is int and value['id'] > after, 'Invalid controller event cursor.')
                        rows.append(value)
                        after = value['id']
                        if len(rows) >= limit:
                            more = True
                            break
    except TimeoutError:
        more = True
    return rows, after, more


def prompt(marker, unicode_text, guest_directory):
    return (
        'Complete this acceptance case in the Hyper-V guest using real native tools. '
        'Use the first-party desktop MCP tool named desktop, discovered through ALL_TOOLS. '
        'Do not use @oai/sky, browser Calculator pages, a host tool, or another model. '
        '1. Call desktop status and verify the guest desktop is elevated, interactive and unlocked. '
        '2. Call desktop windows, launch Windows Calculator with desktop launch (application calc.exe), '
        'then refresh windows and select its returned window identity. '
        '3. Use desktop inspect and action to calculate 37 multiplied by 19 in the actual Calculator. '
        'Prefer exposed semantic UIA buttons; if necessary use actual keyboard input through desktop action. '
        'Inspect the final Calculator display and verify it says 703. Do not substitute arithmetic performed by shell code or a claimed answer. '
        '4. Capture Calculator through desktop capture and inspect the returned image once. '
        'The capture returns a path in this task\'s artifact directory. '
        f'5. Using guest native code/file tools, create {guest_directory}\\unicode-evidence.txt with the exact UTF-8 text '
        + json.dumps(unicode_text, ensure_ascii=False)
        + '. Independently read its bytes/text back and verify them. Copy that file into the SAME directory as the capture PNG, named unicode-evidence.txt. '
        '6. In that artifact directory write acceptance-evidence.json with marker, hostname (read from Windows), '
        'calculatorResult=703, unicodeText (the exact verified string), and unicodeFileSha256 computed from actual file bytes. '
        f'The marker is {marker}. Preserve the PNG capture as an artifact. '
        'Do not overwrite previous evidence, modify the controller, or declare success without actual tool verification. '
        'Finish with the hostname, Calculator result, Unicode verification and screenshot filename.'
    )


async def run(args):
    binding_path = args.deployment.resolve(strict=True)
    binding = deployment(binding_path)
    verify_binding(binding)
    port = binding.get('controllerPort', 8789)
    require(type(port) is int and 1 <= port <= 65535, 'Binding has an invalid controller port.')
    args.url = args.url or ('http://127.0.0.1:' + str(port))
    home = args.home or (Path(binding['controllerHome']) if binding.get('controllerHome') else None)
    require(home is not None, 'An explicit controller home or binding.controllerHome is required.')
    resolved_home = home.resolve()
    resolved_output = args.output.resolve()
    require(resolved_output != resolved_home and resolved_home not in resolved_output.parents,
            'Acceptance evidence must be outside controller history/data.')
    marker = 'SANDBOX-ACCEPTANCE-' + uuid.uuid4().hex[:12]
    unicode_text = marker + ' café 日本語 🚀 — Ελληνικά\n'
    guest_directory = str(PureWindowsPath(args.guest_directory) / marker)
    require(PureWindowsPath(guest_directory).is_absolute(), 'Guest working directory must be an absolute Windows path.')
    directory = args.output.resolve() / marker
    directory.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'codex-permissive-sandbox-acceptance/v1', 'startedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'marker': marker, 'vmId': uuid_text(binding['vmId']), 'environmentId': uuid_text(binding['sandboxId']),
              'guestWorkingDirectory': guest_directory, 'expectedModel': args.expected_model, 'passed': False}
    request_body = {'workingDirectory': guest_directory, 'browserMode': 'dedicated', 'browser': 'chromium',
                    'submissionId': str(uuid.uuid4()), 'prompt': prompt(marker, unicode_text, guest_directory)}
    write_json(directory / 'request.json', request_body)
    report_path = directory / 'verification.json'
    write_json(report_path, report)
    if args.prepare_only:
        report['preparedOnly'] = True
        write_json(report_path, report)
        print(json.dumps({'preparedOnly': True, 'request': str(directory / 'request.json'), 'evidence': str(report_path)}, indent=2))
        return report
    task = None
    try:
        async with SignedSession(home=home, private_key=args.private_key, timeout=ClientTimeout(total=args.request_timeout, connect=10),
                                 read_bufsize=16 * 1024 * 1024) as client:
            async def api(method, route, body=None):
                async with client.request(method, args.url.rstrip('/') + route, json=body) as response:
                    text = await response.text()
                    require(response.status < 400, f'Controller HTTP {response.status}: {text[:1000]}')
                    return json.loads(text)

            health = await api('GET', '/v1/health')
            environment = await api('GET', '/v1/environment')
            verify_controller(health, environment, binding)
            report.update(controllerHealth=health, environmentBefore=environment)
            require(health.get('activeTasks', 0) == 0, 'The sandbox already has active work; acceptance will not interleave with it.')
            # This probe is fixed read-only code and also confirms host elevation,
            # the exact VM binding and the configured perimeter before submission.
            before = await fixed_probe(binding_path, None, directory, args.probe_timeout)
            require(before.get('hostname') and before['hostname'].lower() != socket.gethostname().lower(), 'Preflight did not identify a distinct guest computer.')
            report['guestPreflight'] = before
            task = await api('POST', '/v1/tasks', request_body)
            report['taskId'] = task['id']
            write_json(report_path, report)
            print('Sandbox acceptance task: ' + task['id'], flush=True)
            started = time.monotonic()
            events, cursor = [], 0
            while True:
                task = await api('GET', '/v1/tasks/' + task['id'])
                if task.get('execution'):
                    verify_execution(task, binding)
                rows, cursor, more = await events_page(client, args.url.rstrip('/') + '/v1/tasks/' + task['id'] + '/events', cursor)
                events.extend(rows)
                if task['status'] in TERMINAL:
                    async with asyncio.timeout(args.request_timeout):
                        while more:
                            rows, cursor, more = await events_page(client, args.url.rstrip('/') + '/v1/tasks/' + task['id'] + '/events', cursor, seconds=5)
                            events.extend(rows)
                    break
                if args.timeout is not None and time.monotonic() - started >= args.timeout:
                    if not args.keep_running_on_timeout:
                        report['cancellationRequested'] = True
                        try:
                            report['cancellation'] = await api('POST', '/v1/tasks/' + task['id'] + '/cancel', {})
                        except Exception as error:
                            report['cancellationError'] = str(error)
                    raise AcceptanceFailure('Acceptance wait exceeded its configured timeout.')
                await asyncio.sleep(args.poll_interval)
            report['taskStatus'], report['taskError'] = task['status'], task.get('error')
            report['execution'] = verify_execution(task, binding)
            require(task['status'] == 'completed', 'Guest model task did not complete successfully.')
            desktop = desktop_evidence(events)
            report['nativeDesktop'] = desktop
            native_thread = str(uuid.UUID(task['threadId']))
            probe = await fixed_probe(binding_path, native_thread, directory, args.probe_timeout)
            write_json(directory / 'model-context-probe.json', probe)
            report['nativeModel'] = verify_probe(probe, binding, native_thread, args.expected_model)
            require(probe['hostname'] == before['hostname'], 'Guest hostname changed during the acceptance attempt.')
            artifacts = {item['id']: item for item in task.get('artifacts', [])}
            for required in ('unicode-evidence.txt', 'acceptance-evidence.json'):
                require(required in artifacts, 'Missing guest artifact: ' + required)
            wanted = [name for name in artifacts if name.endswith('.png')] + ['unicode-evidence.txt', 'acceptance-evidence.json']
            require(any(name.endswith('.png') for name in wanted), 'No screenshot PNG artifact was exported.')
            downloaded = {}
            for name in wanted:
                require(Path(name).name == name and not any(char in name for char in '/\\:'), 'Unsafe artifact identity.')
                async with client.get(args.url.rstrip('/') + '/v1/tasks/' + task['id'] + '/artifacts/' + name) as response:
                    require(response.status == 200, 'Controller could not export ' + name)
                    data = await response.read()
                (directory / name).write_bytes(data)
                downloaded[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                if name.endswith('.png'):
                    require(data.startswith(PNG_SIGNATURE) and len(data) >= 24, 'Invalid PNG screenshot bytes.')
                    width, height = struct.unpack('>II', data[16:24])
                    require(width > 10 and height > 10, 'Screenshot dimensions are not a usable desktop image.')
                    downloaded[name].update(width=width, height=height)
            actual_unicode = (directory / 'unicode-evidence.txt').read_bytes()
            require(actual_unicode == unicode_text.encode('utf-8'), 'Unicode file bytes differ from the exact required UTF-8 text.')
            guest_evidence = json.loads((directory / 'acceptance-evidence.json').read_text(encoding='utf-8-sig'))
            require(guest_evidence.get('marker') == marker and guest_evidence.get('calculatorResult') == 703, 'Guest evidence marker/calculator mismatch.')
            require(guest_evidence.get('hostname', '').lower() == probe['hostname'].lower(), 'Agent hostname does not match the independent VM probe.')
            require(guest_evidence.get('unicodeText') == unicode_text, 'Guest evidence does not contain verified Unicode text.')
            require(guest_evidence.get('unicodeFileSha256', '').lower() == hashlib.sha256(actual_unicode).hexdigest(), 'Guest evidence file hash mismatch.')
            report.update(passed=True, artifacts=downloaded, guestEvidence=guest_evidence, environmentAfter=await api('GET', '/v1/environment'))
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        report['finishedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write_json(report_path, report)
        print(json.dumps({'passed': report['passed'], 'taskId': report.get('taskId'), 'evidence': str(report_path), 'failure': report.get('failure')}, indent=2), flush=True)
    return report


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, required=True, help='Explicit host controller deployment binding.')
    parser.add_argument('--home', type=Path, help='Controller data home; otherwise binding.controllerHome.')
    parser.add_argument('--url', help='Signed controller origin; defaults to binding.controllerPort on loopback.')
    parser.add_argument('--private-key', type=Path)
    parser.add_argument('--output', type=Path, default=Path('C:/codex-build/permissive-sandbox-acceptance'))
    parser.add_argument('--guest-directory', default='C:/workspaces/acceptance')
    parser.add_argument('--expected-model', default=MODEL)
    parser.add_argument('--timeout', type=float, default=1200, help='Maximum model wait seconds; 0 means no acceptance deadline.')
    parser.add_argument('--request-timeout', type=float, default=60)
    parser.add_argument('--probe-timeout', type=float, default=90)
    parser.add_argument('--poll-interval', type=float, default=2)
    parser.add_argument('--keep-running-on-timeout', action='store_true')
    parser.add_argument('--prepare-only', action='store_true', help='Write the concrete request/evidence skeleton without connecting or submitting a task.')
    args = parser.parse_args()
    if args.timeout < 0 or min(args.request_timeout, args.probe_timeout, args.poll_interval) <= 0:
        parser.error('Timeouts and polling must be positive; --timeout 0 disables only the acceptance wait deadline.')
    args.timeout = args.timeout or None
    return args


if __name__ == '__main__':
    asyncio.run(run(arguments()))
