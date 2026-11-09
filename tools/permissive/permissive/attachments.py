"""Private managed copies of chat inputs; original user files are never modified."""

import base64
import binascii
import json
from pathlib import Path
import re
import uuid

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 10


def content_name(name):
    suffix = Path(name).suffix.lower()
    return 'content' + (suffix if re.fullmatch(r'\.[a-z0-9]{1,16}', suffix) else '')


def attachment_id(value):
    if not isinstance(value, str):
        raise ValueError('Attachment ID must be a UUID')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise ValueError('Attachment ID must be a UUID') from None
    return value


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


class Attachments:
    def __init__(self, store):
        self.store = store
        store.db.execute('CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, workspace_id TEXT NOT NULL, owner_task_id TEXT)')
        store.db.commit()

    def path(self, ident, name=None):
        from .secure_delete import checked
        if name is None:
            row = self.store.db.execute('SELECT name FROM attachments WHERE id=?', (attachment_id(ident),)).fetchone()
            if not row:
                raise ValueError('Attachment is unavailable; attach it again')
            name = row['name']
        return checked(self.store.home / 'attachments' / attachment_id(ident) / content_name(name), self.store.home)

    def get(self, ident):
        row = self.store.db.execute('SELECT * FROM attachments WHERE id=?', (attachment_id(ident),)).fetchone()
        if not row or not self.path(ident).is_file():
            raise ValueError('Attachment is unavailable; attach it again')
        return dict(row)

    @staticmethod
    def descriptor(row):
        return {key: row[key] for key in ('id', 'name', 'mime', 'size')}

    def upload(self, body):
        if not isinstance(body, dict) or body.keys() != {'name', 'data', 'workspaceId'}:
            raise ValueError('Attachment requires name, base64 data, and workspaceId')
        name = body['name']
        if not isinstance(name, str) or not name.strip() or len(name) > 255 or any(ord(char) < 32 or char in '/\\' for char in name):
            raise ValueError('Invalid attachment filename')
        self.store.workspace(body['workspaceId'])
        encoded = body['data']
        if not isinstance(encoded, str) or not encoded or len(encoded) > (MAX_FILE_BYTES + 2) // 3 * 4:
            raise ValueError('Files must be nonempty and at most 16 MiB')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('Invalid attachment data') from None
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError('Files must be nonempty and at most 16 MiB')
        ident = str(uuid.uuid4())
        path = self.path(ident, name)
        path.parent.mkdir(parents=True)
        try:
            path.write_bytes(data)
            with self.store.db:
                self.store.db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,NULL)', (ident, name, image_type(data), len(data), body['workspaceId']))
        except BaseException:
            from .secure_delete import erase_tree
            erase_tree(path.parent, self.store.home)
            raise
        return self.descriptor(self.get(ident))

    def validate(self, ids, workspace_id, conversation=None):
        if not isinstance(ids, list) or len(ids) > MAX_FILES or any(not isinstance(ident, str) for ident in ids) or len(set(ids)) != len(ids):
            raise ValueError('Attach at most 10 distinct files')
        total = 0
        for ident in ids:
            row = self.get(ident)
            if row['workspace_id'] != workspace_id:
                raise ValueError('Attachment belongs to a different workspace')
            shared = conversation and self.store.db.execute('SELECT 1 FROM segment_attachments a JOIN chat_segments s ON s.file_id=a.file_id WHERE a.attachment_id=? AND s.chat_id=?', (ident, conversation)).fetchone()
            if row['owner_task_id'] and not shared and self.store.get(row['owner_task_id'])['conversationId'] != conversation:
                raise ValueError('Attachment already belongs to a different chat')
            total += row['size']
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Attachments exceed the 32 MiB message limit')

    def claim(self, ids, task_id):
        with self.store.db:
            for ident in ids:
                self.store.db.execute('UPDATE attachments SET owner_task_id=? WHERE id=? AND owner_task_id IS NULL', (task_id, ident))

    def remove_draft(self, ident):
        row = self.get(ident)
        if row['owner_task_id']:
            raise ValueError('Sent attachments are kept with their chat')
        from .secure_delete import erase_tree
        erase_tree(self.path(ident).parent, self.store.home)
        with self.store.db:
            self.store.db.execute('DELETE FROM attachments WHERE id=?', (ident,))


def native_input(home, prompt, ids):
    """Images enter native vision input; other files can be read by agent tools."""
    import sqlite3
    from contextlib import closing
    from .secure_delete import checked
    items, files = [], []
    with closing(sqlite3.connect(home / 'tasks.sqlite')) as database:
        database.row_factory = sqlite3.Row
        for ident in ids:
            row = database.execute('SELECT * FROM attachments WHERE id=?', (attachment_id(ident),)).fetchone()
            if row is None:
                raise ValueError('Attached file is unavailable')
            path = checked(home / 'attachments' / ident / content_name(row['name']), home)
            if not path.is_file():
                raise ValueError('Attached file is unavailable')
            files.append({'name': row['name'], 'path': str(path), 'bytes': row['size']})
            if row['mime'].startswith('image/'):
                items.append({'type': 'localImage', 'path': str(path)})
    if files:
        prompt += '\n\nAttached files (managed copies on the execution host; use tools to read their contents):\n' + json.dumps(files, ensure_ascii=False)
    return [{'type': 'text', 'text': prompt, 'text_elements': []}, *items]
