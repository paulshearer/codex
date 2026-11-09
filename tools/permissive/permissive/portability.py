"""Machine-independent controller snapshots; guest disks are exported by Hyper-V."""

import argparse
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3

from .vm_runtime import atomic_json


DIRECTORIES = ('artifacts', 'attachments', 'skills', 'logs', 'chat-segments')
DATABASES = ('tasks.sqlite', 'auth/replay.sqlite')
SKILL_METADATA = 'portable-skills.json'
CONTROLLER_PREFIX = '@controller/'


def has_column(database, table, column):
    return column in {row[1] for row in database.execute('PRAGMA table_info("' + table + '")')}


def skill_key(value):
    return os.path.normcase(os.path.abspath(value))


def skill_paths(encoded):
    values = json.loads(encoded)
    if not isinstance(values, list) or any(not isinstance(path, str) for path in values):
        raise ValueError('Invalid configured skill paths in controller state')
    return values


def safe_skill_files(root):
    """Inspect package entries before descending; never traverse a reparse dir."""
    root = Path(root)
    if root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('Portable skills cannot contain symbolic links or reparse points')
    files, pending = [], [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Portable skills cannot contain symbolic links or reparse points')
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files.append(path)
            else:
                raise ValueError('Portable skills require regular supporting files')
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def bundle_skill(value, snapshot):
    from .workspaces import skill_document
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError('Configured skill is unavailable; update its workspace/library selection before export: ' + str(value))
    root = path.parent
    files = safe_skill_files(root)
    skill_document(value)
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    destination = snapshot / 'skills' / 'imported' / digest.hexdigest()
    if destination.exists():
        copied = safe_skill_files(destination)
        expected = [(file.relative_to(root).as_posix(), hashlib.sha256(file.read_bytes()).digest()) for file in files]
        actual = [(file.relative_to(destination).as_posix(), hashlib.sha256(file.read_bytes()).digest()) for file in copied]
        if expected != actual:
            raise ValueError('Portable skill destination conflicts with existing package data')
    else:
        destination.mkdir(parents=True)
        for file in files:
            target = destination / file.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    return CONTROLLER_PREFIX + (destination / path.name).relative_to(snapshot).as_posix()


def rewrite_context(encoded, mapping, importing=False):
    context = json.loads(encoded)
    if not isinstance(context, dict) or not isinstance(context.get('skills', []), list):
        raise ValueError('Invalid captured workspace context')
    for skill in context.get('skills', []):
        if not isinstance(skill, dict):
            raise ValueError('Invalid captured skill document')
        previous = skill.get('path')
        key = previous if importing else skill_key(previous) if isinstance(previous, str) else None
        if key in mapping:
            skill.setdefault('sourcePath', previous)
            skill['path'] = mapping[key]
            skill.pop('portableUnavailable', None)
        else:
            # Historical text remains readable, but a stale path must not later
            # resolve to unrelated files on the destination controller.
            if isinstance(previous, str):
                skill.setdefault('sourcePath', previous)
            skill['path'] = None
            skill['portableUnavailable'] = True
    return json.dumps(context, ensure_ascii=False)


def rewrite_database_skills(database, mapping, importing=False):
    convert = (lambda path: mapping[path]) if importing else (lambda path: mapping[skill_key(path)])
    if has_column(database, 'workspaces', 'skills'):
        for ident, encoded in database.execute('SELECT id,skills FROM workspaces').fetchall():
            paths = list(dict.fromkeys(convert(path) for path in skill_paths(encoded)))
            database.execute('UPDATE workspaces SET skills=? WHERE id=?', (json.dumps(paths), ident))
    if has_column(database, 'skill_library', 'path'):
        rows = database.execute('SELECT path,category FROM skill_library ORDER BY path').fetchall()
        database.execute('DELETE FROM skill_library')
        for path, category in rows:
            database.execute('INSERT OR IGNORE INTO skill_library(path,category) VALUES (?,?)', (convert(path), category))
    if has_column(database, 'tasks', 'workspace_context'):
        for ident, encoded in database.execute('SELECT id,workspace_context FROM tasks WHERE workspace_context IS NOT NULL').fetchall():
            database.execute('UPDATE tasks SET workspace_context=? WHERE id=?', (rewrite_context(encoded, mapping, importing), ident))
    if has_column(database, 'tasks', 'request'):
        for ident, encoded in database.execute('SELECT id,request FROM tasks').fetchall():
            request = json.loads(encoded)
            if 'skills' in request:
                paths = skill_paths(json.dumps(request['skills']))
                selected, unavailable = [], []
                for path in paths:
                    key = path if importing else skill_key(path)
                    (selected if key in mapping else unavailable).append(mapping[key] if key in mapping else path)
                request['skills'] = list(dict.fromkeys(selected))
                if unavailable:
                    request.setdefault('archivedUnavailableSkills', unavailable)
                database.execute('UPDATE tasks SET request=? WHERE id=?', (json.dumps(request, ensure_ascii=False), ident))


def export_skills(snapshot):
    database_path = snapshot / 'tasks.sqlite'
    mapping, entries = {}, {}
    if database_path.is_file():
        with closing(sqlite3.connect(database_path)) as database, database:
            active, historical = set(), set()
            if has_column(database, 'workspaces', 'skills'):
                for (encoded,) in database.execute('SELECT skills FROM workspaces'):
                    active.update(skill_paths(encoded))
            if has_column(database, 'skill_library', 'path'):
                active.update(row[0] for row in database.execute('SELECT path FROM skill_library'))
            if has_column(database, 'tasks', 'workspace_context'):
                for (encoded,) in database.execute('SELECT workspace_context FROM tasks WHERE workspace_context IS NOT NULL'):
                    context = json.loads(encoded)
                    historical.update(skill['path'] for skill in context.get('skills', []) if isinstance(skill.get('path'), str))
            if has_column(database, 'tasks', 'request'):
                for (encoded,) in database.execute('SELECT request FROM tasks'):
                    request = json.loads(encoded)
                    historical.update(skill_paths(json.dumps(request.get('skills', []))))
            for value in sorted(active | historical):
                if value not in active and (not Path(value).is_absolute() or not Path(value).is_file()):
                    continue
                placeholder = bundle_skill(value, snapshot)
                mapping[skill_key(value)] = placeholder
                entries.setdefault(placeholder, []).append(value)
            rewrite_database_skills(database, mapping)
    rewrite_segment_skills(snapshot, mapping)
    atomic_json(snapshot / SKILL_METADATA, {'schema': 'codex-permissive-portable-skills/v1',
        'entries': [{'path': path, 'sourcePaths': sources} for path, sources in sorted(entries.items())]})


def rewrite_segment_skills(root, mapping, importing=False):
    for file in (root / 'chat-segments').glob('*.sqlite'):
        with closing(sqlite3.connect(file)) as database, database:
            for ident, encoded, value in database.execute('SELECT task_id,context,value FROM turns').fetchall():
                task = json.loads(value)
                request = task.get('request', {})
                if 'skills' in request:
                    selected = []
                    for path in request['skills']:
                        key = path if importing else skill_key(path)
                        if key in mapping:
                            selected.append(mapping[key])
                        else:
                            request.setdefault('archivedUnavailableSkills', []).append(path)
                    request['skills'] = selected
                database.execute('UPDATE turns SET context=?,value=? WHERE task_id=?',
                    (rewrite_context(encoded, mapping, importing), json.dumps(task, ensure_ascii=False), ident))


def import_skill_mapping(source, home, verified_names):
    metadata = source / SKILL_METADATA
    entries = []
    if SKILL_METADATA in verified_names:
        value = json.loads(metadata.read_text(encoding='utf-8'))
        if value.get('schema') != 'codex-permissive-portable-skills/v1' or not isinstance(value.get('entries'), list):
            raise ValueError('Invalid portable skill metadata')
        entries = value['entries']
    mapping = {}
    for entry in entries:
        path = entry.get('path') if isinstance(entry, dict) else None
        if not isinstance(path, str) or not re.fullmatch(r'@controller/skills/imported/[0-9a-f]{64}/[Ss][Kk][Ii][Ll][Ll]\.[Mm][Dd]', path):
            raise ValueError('Invalid portable skill path')
        relative = path[len(CONTROLLER_PREFIX):]
        if relative not in verified_names or path in mapping:
            raise ValueError('Portable skill metadata references missing or duplicate package data')
        mapping[path] = str(home.joinpath(*relative.split('/')))
    database_path = source / 'tasks.sqlite'
    if 'tasks.sqlite' in verified_names:
        with closing(sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True)) as database:
            active = []
            if has_column(database, 'workspaces', 'skills'):
                for (encoded,) in database.execute('SELECT skills FROM workspaces'):
                    active.extend(skill_paths(encoded))
            if has_column(database, 'skill_library', 'path'):
                active.extend(row[0] for row in database.execute('SELECT path FROM skill_library'))
            if any(path not in mapping for path in active):
                raise ValueError('Configured skill is outside portable skill metadata; import cannot read destination host paths')
    return mapping


def backup_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as reader, closing(sqlite3.connect(target)) as writer:
        reader.backup(writer)


def export_state(home, output):
    home, output = Path(home).resolve(strict=True), Path(output).resolve()
    if output == home or home in output.parents:
        raise ValueError('Snapshot output must be outside controller data')
    state_path = home / 'environment.json'
    state = json.loads(state_path.read_text())
    if state.get('phase') != 'exported':
        raise ValueError('Fence the source through /v1/environment/export before taking a portable snapshot')
    output.mkdir(parents=True, exist_ok=False)
    for name in DATABASES:
        source = home / name
        if source.is_file():
            backup_database(source, output / name)
    registry = home / 'auth/authorized-keys.json'
    if registry.is_file():
        public = json.loads(registry.read_text(encoding='utf-8-sig'))
        # Local private keys are source-user DPAPI material and are reissued.
        public['keys'] = [entry for entry in public['keys'] if entry.get('name') not in {'Local api client', 'Local browser client', 'Local companion API client'}]
        atomic_json(output / 'auth/authorized-keys.json', public)
    for name in DIRECTORIES:
        source = home / name
        if source.is_dir():
            if source.is_symlink() or getattr(source.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Controller export cannot follow filesystem reparse points')
            for path in source.rglob('*'):
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Controller export cannot follow filesystem reparse points')
            shutil.copytree(source, output / name)
    export_skills(output)
    shutil.copy2(state_path, output / 'environment.json')
    hashes = {str(path.relative_to(output)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in output.rglob('*') if path.is_file()}
    atomic_json(output / 'controller-manifest.json', {'schema': 'codex-permissive-controller-state/v1',
        'environmentId': state['environmentId'], 'generation': state['generation'], 'files': hashes})
    return {'snapshot': str(output), 'files': len(hashes)}


def import_state(source, home, vm_id):
    source, home = Path(source).resolve(strict=True), Path(home).resolve()
    manifest = json.loads((source / 'controller-manifest.json').read_text())
    if manifest.get('schema') != 'codex-permissive-controller-state/v1':
        raise ValueError('Unsupported portable controller state')
    if home.exists() and any(home.iterdir()):
        raise ValueError('Import controller home must be empty; existing history is never overwritten')
    verified = []
    for name, digest in manifest['files'].items():
        portable = PureWindowsPath(name)
        if portable.is_absolute() or portable.drive or '..' in portable.parts or not portable.parts:
            raise ValueError('Invalid portable controller path')
        normalized = '/'.join(portable.parts)
        if normalized not in {*DATABASES, 'auth/authorized-keys.json', 'environment.json', SKILL_METADATA} and not any(normalized.startswith(directory + '/') for directory in DIRECTORIES):
            raise ValueError('Unsupported portable controller path')
        raw = source.joinpath(*portable.parts)
        if any(parent.is_symlink() or getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400 for parent in [raw, *raw.parents] if parent != source and source in parent.parents):
            raise ValueError('Invalid portable controller path')
        path = raw.resolve(strict=True)
        target = home.joinpath(*portable.parts).resolve()
        if source not in path.parents or home not in target.parents or not path.is_file():
            raise ValueError('Invalid portable controller path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Portable controller file hash mismatch')
        verified.append((normalized, path))
    skill_mapping = import_skill_mapping(source, home, {name for name, _ in verified})
    home.mkdir(parents=True, exist_ok=True)
    for name, path in verified:
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    if (home / 'tasks.sqlite').is_file():
        with closing(sqlite3.connect(home / 'tasks.sqlite')) as database, database:
            rewrite_database_skills(database, skill_mapping, importing=True)
    rewrite_segment_skills(home, skill_mapping, importing=True)
    state = json.loads((home / 'environment.json').read_text())
    state.update(vmId=vm_id, generation=int(state['generation']) + 1, phase='ready')
    atomic_json(home / 'environment.json', state)
    return {'environmentId': state['environmentId'], 'generation': state['generation'], 'home': str(home)}


def seal_secret(value, password):
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    salt, nonce = os.urandom(16), os.urandom(12)
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    data = AESGCM(key).encrypt(nonce, value.encode(), b'permissive-migration-secret/v1')
    return {'schema': 'permissive-migration-secret/v1', **{name: base64.b64encode(raw).decode()
        for name, raw in [('salt', salt), ('nonce', nonce), ('data', data)]}}


def open_secret(envelope, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if envelope.get('schema') != 'permissive-migration-secret/v1':
        raise ValueError('Unsupported credential envelope')
    salt, nonce, data = (base64.b64decode(envelope[key], validate=True) for key in ('salt', 'nonce', 'data'))
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    return AESGCM(key).decrypt(nonce, data, b'permissive-migration-secret/v1').decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--export-state', action='store_true')
    parser.add_argument('--import-state', action='store_true')
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source', '--input', type=Path)
    parser.add_argument('--vm-id')
    args = parser.parse_args()
    if args.export_state:
        value = export_state(args.home, args.output)
    elif args.import_state:
        value = import_state(args.source, args.home, args.vm_id)
    else:
        parser.error('Select export-state or import-state')
    print(json.dumps(value))


if __name__ == '__main__':
    main()
