"""Durable jobs and replayable event streams, independent from native rollouts."""

import json
import sqlite3
import time
import uuid
from pathlib import Path, PureWindowsPath

from .workspaces import directory_key, normalize_workspace, workspace_context, compose_instructions, MAX_CONTEXT_BYTES
from .telemetry import UsageTracker
from .diagnostics import WorkspaceDiagnostics

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Store:
    @classmethod
    def worker_connection(cls, home, guest_paths=False):
        """Open an existing WAL database without running startup recovery."""
        store = cls.__new__(cls)
        store.home, store.guest_paths = home, guest_paths
        store.recover_segment_branches = False
        store.diagnostics = WorkspaceDiagnostics(home)
        store.db = sqlite3.connect(home / 'tasks.sqlite', timeout=30)
        store.db.row_factory = sqlite3.Row
        store.db.execute('PRAGMA foreign_keys=ON')
        from .segments import Segments
        store.segments = Segments(store)
        return store

    def __init__(self, home, guest_paths=False):
        self.segments = None
        self.home = home
        self.guest_paths = guest_paths
        self.diagnostics = WorkspaceDiagnostics(home)
        home.mkdir(parents=True, exist_ok=True)
        from .secure_delete import recover_databases
        recover_databases(home)
        self.db = sqlite3.connect(home / "tasks.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        from .reply_versions import initialize
        initialize(self.db)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS skill_library (path TEXT PRIMARY KEY, category TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, directory TEXT NOT NULL,
                directory_key TEXT NOT NULL, system_prompt TEXT NOT NULL, skills TEXT NOT NULL,
                browser_mode TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                thread_id TEXT, result TEXT, error TEXT, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, idempotency_key TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
            CREATE TABLE IF NOT EXISTS followups (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                conversation_id TEXT NOT NULL, body TEXT NOT NULL,
                request TEXT,
                status TEXT NOT NULL DEFAULT 'waiting', task_id TEXT, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS steering (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, response TEXT
            );
            CREATE TABLE IF NOT EXISTS conversation_queues (
                conversation_id TEXT PRIMARY KEY, held INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS conversation_metadata (
                conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history_deletions (
                source_id TEXT PRIMARY KEY, replacement_id TEXT NOT NULL,
                guest_erased INTEGER NOT NULL DEFAULT 0
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        with self.db:
            followup_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(followups)")}
            if "request" not in followup_columns:
                self.db.execute("ALTER TABLE followups ADD COLUMN request TEXT")
            workspace_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(workspaces)")}
            for column, default in (("execution_detail", "standard"), ("diagnostic_log_level", "off")):
                if column not in workspace_columns:
                    self.db.execute(f"ALTER TABLE workspaces ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
            for column in ("conversation_id", "active_turn_id", "diagnostic_log_level"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
            if "history_seed" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN history_seed TEXT")
            if "browser_settings" not in workspace_columns:
                self.db.execute("ALTER TABLE workspaces ADD COLUMN browser_settings TEXT NOT NULL DEFAULT '{\"browser\":\"chromium\",\"browserProfile\":\"\"}'")
            if "workspace_id" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN workspace_id TEXT REFERENCES workspaces(id)")
            if "workspace_context" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN workspace_context TEXT")
            if "completed_at" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN completed_at INTEGER")
                self.db.execute("UPDATE tasks SET completed_at=updated_at WHERE status IN ('completed','failed','cancelled','interrupted')")
            if "token_usage" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN token_usage TEXT")
                # Recover saved counters for old chats without changing their events.
                for task in self.db.execute("SELECT id FROM tasks").fetchall():
                    tracker, recovered = UsageTracker(), None
                    for event in self.db.execute("SELECT data FROM events WHERE task_id=? AND kind='agent' AND (data LIKE '%thread/tokenUsage/updated%' OR data LIKE '%turn/started%') ORDER BY id", (task["id"],)):
                        recovered = tracker.observe(json.loads(event["data"])) or recovered
                    if recovered:
                        self.db.execute("UPDATE tasks SET token_usage=? WHERE id=?", (json.dumps(recovered), task["id"]))
            for task in self.db.execute("SELECT id,request FROM tasks WHERE workspace_id IS NULL").fetchall():
                request = json.loads(task["request"])
                workspace = self.workspace_for_directory(request.get("workingDirectory", str(home)), request.get("browserMode", "dedicated"))
                request["workspaceId"] = workspace["id"]
                self.db.execute("UPDATE tasks SET workspace_id=?,workspace_context=?,request=? WHERE id=?",
                                (workspace["id"], json.dumps({"systemPrompt": "", "skills": [], "revision": 1}), json.dumps(request), task["id"]))
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_workspace ON tasks(workspace_id)")
            if not self.db.execute("SELECT 1 FROM workspaces LIMIT 1").fetchone():
                self.workspace_for_directory(r'C:\workspaces' if self.guest_paths else str(Path('C:/git') if Path('C:/git').is_dir() else home), name="General")
            conversations, threads = {}, {}
            for row in self.db.execute("SELECT id,request,thread_id,workspace_id,conversation_id FROM tasks ORDER BY rowid").fetchall():
                parent = json.loads(row["request"]).get("resumeTaskId")
                thread_key = (row["workspace_id"], row["thread_id"])
                conversation = row["conversation_id"] or conversations.get(parent) or (threads.get(thread_key) if row["thread_id"] else None) or row["id"]
                conversations[row["id"]] = conversation
                if row["thread_id"]:
                    threads[thread_key] = conversation
                self.db.execute("UPDATE tasks SET conversation_id=?, diagnostic_log_level=coalesce(diagnostic_log_level,'off'), active_turn_id=NULL WHERE id=?", (conversation, row["id"]))
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_conversation ON tasks(conversation_id)")
            # Startup never silently resumes pending user work.
            self.db.execute("INSERT OR REPLACE INTO conversation_queues SELECT DISTINCT conversation_id,1 FROM followups WHERE status='waiting'")
            self.db.execute("UPDATE steering SET status='uncertain' WHERE status='pending'")
            self.db.execute("""UPDATE followups SET status='dispatched',task_id=(
                SELECT id FROM tasks WHERE idempotency_key='followup:' || followups.id)
                WHERE status='waiting' AND EXISTS (SELECT 1 FROM tasks WHERE idempotency_key='followup:' || followups.id)""")
        from .segments import Segments
        self.segments = Segments(self)
        for task in self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('running','queued','pausing','cancelling')"
        ).fetchall():
            self.update(
                task["id"],
                status="interrupted",
                error="Service stopped; continue explicitly.",
            )

    def _get_legacy(self, ident):
        from .stream_recovery import is_recoverable_stream_error
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        value["request"] = json.loads(value["request"])
        goal = self.db.execute("SELECT data FROM events WHERE task_id=? AND kind='goal' ORDER BY id DESC LIMIT 1", (ident,)).fetchone()
        return {
            "id": value["id"],
            "status": value["status"],
            "request": value["request"],
            "threadId": value["thread_id"],
            "conversationId": value["conversation_id"],
            "activeTurnId": value["active_turn_id"],
            "diagnosticLogLevel": value["diagnostic_log_level"],
            "result": value["result"],
            "error": value["error"],
            "canRetry": bool(value["status"] == "failed" and value["thread_id"] and is_recoverable_stream_error(value["error"])),
            "createdAt": value["created_at"],
            "updatedAt": value["updated_at"],
            "completedAt": value["completed_at"],
            "tokenUsage": json.loads(value["token_usage"]) if value["token_usage"] else None,
            "workspaceId": value["workspace_id"],
            "workspaceRevision": json.loads(value["workspace_context"])["revision"],
            "goal": json.loads(goal["data"]) if goal and goal['data'] else None,
        }

    def get(self, ident):
        from .stream_recovery import is_recoverable_stream_error
        try:
            value = self._get_legacy(ident)
        except KeyError:
            payload = self.segments.task_payload(ident, task_only=True) if self.segments else None
            if payload:
                return payload['task']
            raise
        if self.segments and self.segments.ready(value['conversationId']):
            with self.db:
                self.segments.sync_pending(value['conversationId'])
            info = self.segments.info(*self.segments.scope) if self.segments.scope and self.segments.scope[0] == value['conversationId'] else None
            payload = self.segments.task_payload(ident, info, task_only=True)
            if payload:
                saved = payload['task']
                for key in ('request', 'result', 'error', 'goal'):
                    value[key] = saved.get(key)
                value['canRetry'] = bool(value['status'] == 'failed' and value['threadId'] and
                    is_recoverable_stream_error(value['error']))
        return value

    def submit(self, request, key=None):
        if key:
            row = self.db.execute(
                "SELECT id, request FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                if self.get(row['id'])['request'] != request:
                    raise ValueError(
                        "Idempotency key was already used for a different request"
                    )
                return self.get(row["id"]), False
        ident, now = str(uuid.uuid4()), int(time.time())
        workspace = self.workspace(request["workspaceId"]) if request.get("workspaceId") else self.workspace_for_directory(request.get("workingDirectory", str(self.home)), request.get("browserMode", "dedicated"))
        context = workspace_context({**workspace, **({"skills": request["skills"]} if "skills" in request else {})})
        context.update(mode=request.get("mode", "default"), goal=request.get("goal", ""), explicitSkills="skills" in request)
        if len(compose_instructions(context).encode()) > MAX_CONTEXT_BYTES:
            raise ValueError("Chat instructions exceed the 1 MiB context limit")
        with self.db:
            conversation = self.get(request["resumeTaskId"])["conversationId"] if request.get("resumeTaskId") else ident
            self.db.execute(
                "INSERT INTO tasks (id,request,status,created_at,updated_at,idempotency_key,workspace_id,workspace_context,conversation_id,diagnostic_log_level) VALUES (?,?,'queued',?,?,?,?,?,?,?)",
                (ident, json.dumps(request), now, now, key, workspace["id"], json.dumps(context), conversation, workspace["diagnosticLogLevel"]),
            )
            if request.get('resumeTaskId'):
                self.db.execute('UPDATE tasks SET history_seed=(SELECT history_seed FROM tasks WHERE id=?) WHERE id=?',
                                (request['resumeTaskId'], ident))
        if self.segments.ready(conversation):
            with self.db:
                self.segments.sync_pending(conversation)
            if request.get('resumeTaskId'):
                self.set_history_seed(ident, self.history_seed(request['resumeTaskId']))
                info = self.segments.info(conversation)
                # A live native thread already owns its compaction checkpoint.
                # Reimport only when there is no native thread to continue.
                if info['number'] > 1 and not self.get(request['resumeTaskId'])['threadId'] and self.history_seed(ident) is None:
                    self.set_history_seed(ident, self.task_context(ident).get('_regenerationSeed', []))
        elif not self.db.execute('SELECT 1 FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.conversation_id=? LIMIT 1', (conversation,)).fetchone():
            self.segments.migrate(conversation)
        self.diagnostics.record(self.get(ident), "submission", {"request": request, "workspaceContext": context})
        self.event(ident, "status", {"status": "queued"})
        return self.get(ident), True

    def update(self, ident, **values):
        allowed = {"status", "thread_id", "result", "error", "active_turn_id"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid task update")
        content = {key: values[key] for key in ('result', 'error') if key in values}
        current = self.get(ident)
        segmented = self.segments.ready(current['conversationId'])
        if segmented and content:
            self.segments.patch_task(ident, **content)
            for key in content:
                values[key] = None
        values["updated_at"] = int(time.time())
        if values.get("status") in TERMINAL:
            values["active_turn_id"] = None
            values["completed_at"] = self.get(ident)["completedAt"] or values["updated_at"]
        elif "status" in values:
            values["completed_at"] = None
        with self.db:
            self.db.execute(
                "UPDATE tasks SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=?",
                (*values.values(), ident),
            )
        self.event(ident, "status", {"status": self.get(ident)["status"], **({"error": content["error"]} if content.get("error") else {})})

    def event(self, ident, kind, data):
        task = self.get(ident)
        segmented = self.segments and self.segments.ready(task['conversationId'])
        info = self.segments.info(task['conversationId']) if segmented else None
        created = time.time()
        with self.db:
            self.db.execute("UPDATE tasks SET updated_at=max(updated_at,?) WHERE id=?", (int(time.time()), ident))
            if kind == "agent" and data.get("method") in {"turn/started", "turn/completed"}:
                turn = data.get("params", {}).get("turn", {})
                if data["method"] == "turn/started":
                    self.db.execute("UPDATE tasks SET active_turn_id=? WHERE id=?", (turn.get("id"), ident))
                else:
                    self.db.execute("UPDATE tasks SET active_turn_id=NULL WHERE id=? AND active_turn_id=?", (ident, turn.get("id")))
            if kind == "usage":
                self.db.execute("UPDATE tasks SET token_usage=? WHERE id=?", (json.dumps(data), ident))
            inserted = self.db.execute(
                "INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)",
                (ident, kind, '' if segmented else json.dumps(data, ensure_ascii=False), created),
            )
            self.db.execute('INSERT INTO event_payload_sizes VALUES (?,?)', (inserted.lastrowid, len(json.dumps(data, ensure_ascii=False).encode('utf-8'))))
            if segmented:
                self.segments.put_event(info, inserted.lastrowid, ident, kind, data, created)
                if kind == 'usage':
                    self.segments.patch_task(ident, tokenUsage=data)
                if kind == 'goal':
                    self.segments.patch_task(ident, goal=data)
        self.diagnostics.record(self.get(ident), kind, data)
        if segmented and kind == 'agent':
            from .segments import compaction
            if compaction(data):
                next_info = self.segments.rotate(task['conversationId'], ident, inserted.lastrowid, data)
                if next_info['id'] != info['id']:
                    self.event(ident, 'history', {'action': 'compaction', 'segmentId': next_info['id']})

    def events(self, ident, after=0):
        task = self.get(ident)
        if self.segments.ready(task['conversationId']):
            info = self.segments.info(task['conversationId'])
            return [{'id': row['id'], 'kind': row['kind'], 'data': json.loads(row['data']), 'createdAt': row['created_at']}
                    for row in self.segments.rows(info, after, limit=200, task_id=ident)]
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "data": json.loads(row["data"]),
                "createdAt": row["created_at"],
            }
            for row in self.db.execute(
                "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 200",
                (ident, after),
            )
        ]

    def list(self, before=None, limit=50, workspace_id=None):
        rows = self.db.execute(
            "SELECT rowid,id FROM tasks WHERE conversation_id NOT IN (SELECT source_id FROM history_deletions) AND (? IS NULL OR rowid<?) AND (? IS NULL OR workspace_id=?) ORDER BY rowid DESC LIMIT ?",
            (before, before, workspace_id, workspace_id, limit + 1),
        ).fetchall()
        return {
            "data": [self.get(row["id"]) for row in rows[:limit]],
            "nextCursor": str(rows[limit - 1]["rowid"]) if len(rows) > limit else None,
        }

    def conversation_tasks(self, ident, segment_id=None, all_segments=False):
        if self.segments and self.segments.ready(ident) and not all_segments:
            with self.db:
                self.segments.sync_pending(ident)
            if segment_id is None and self.segments.scope and self.segments.scope[0] == ident:
                segment_id = self.segments.scope[1]
            return self.segments.tasks(self.segments.info(ident, segment_id))
        rows = self.db.execute("SELECT id FROM tasks WHERE conversation_id=? ORDER BY rowid", (ident,)).fetchall()
        if not rows:
            raise KeyError(ident)
        return [self.get(row["id"]) for row in rows]

    def conversation(self, ident, segment_id=None):
        tasks = self.conversation_tasks(ident, segment_id)
        for task in tasks:
            task["artifacts"] = [{"id": path.name, "size": path.stat().st_size}
                                 for path in (self.home / "artifacts" / task["id"]).glob("*")
                                 if path.is_file() and not path.is_symlink()]
        held = self.db.execute("SELECT held FROM conversation_queues WHERE conversation_id=?", (ident,)).fetchone()
        info = self.segments.info(ident, segment_id) if self.segments else None
        if info:
            for task in tasks:
                if not task.get('historyPromptVisible', True):
                    task['request'] = {**task['request'], 'prompt': '', 'attachments': []}
        from .reply_versions import metadata
        return {"id": ident, "title": self.conversation_title(ident, tasks), "tasks": tasks, "queueHeld": bool(held and held["held"]),
                **metadata(self, ident, tasks, info),
                "segment": {**{key: info[key] for key in ('id', 'number', 'latestId', 'previousId')}, 'after': info['start_event'],
                            'checkpointStatus': self.segments.checkpoint(info).get('status', 'ready')} if info else None,
                "cleanupPending": bool(self.db.execute('SELECT 1 FROM history_deletions WHERE replacement_id=?', (ident,)).fetchone()),
                "followups": [dict(row) | {"body": json.loads(row["body"])} for row in self.db.execute(
                    "SELECT id,body,status,task_id AS taskId,created_at AS createdAt FROM followups WHERE conversation_id=? AND status='waiting' ORDER BY sequence", (ident,))],
                "eventCursor": self.segments.cursor(info) if info else self.db.execute("SELECT coalesce(max(e.id),0) FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.conversation_id=?", (ident,)).fetchone()[0]}

    def conversations(self, cursor=None, limit=50, workspace_id=None):
        boundary_time, boundary_id = (None, None)
        if cursor:
            try:
                boundary_time, boundary_id = json.loads(cursor)
                if not isinstance(boundary_time, int) or not isinstance(boundary_id, str):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("Invalid conversation cursor") from None
        rows = self.db.execute("""
            WITH branches AS (SELECT conversation_id, max(updated_at) AS activity,
                coalesce((SELECT family_id FROM chat_families WHERE branch_id=conversation_id),conversation_id) AS family
                FROM tasks
            WHERE (? IS NULL OR workspace_id=?) AND conversation_id NOT IN
                (SELECT conversation_id FROM tasks WHERE idempotency_key LIKE 'history-replacement:%')
                AND conversation_id NOT IN (SELECT source_id FROM history_deletions) GROUP BY conversation_id),
            ranked AS (SELECT *,row_number() OVER (PARTITION BY family ORDER BY activity DESC,
                (SELECT max(rowid) FROM tasks WHERE tasks.conversation_id=branches.conversation_id) DESC) AS rank FROM branches)
            SELECT * FROM ranked WHERE rank=1
            AND (? IS NULL OR activity<? OR (activity=? AND conversation_id<?))
            ORDER BY activity DESC,conversation_id DESC LIMIT ?
        """, (workspace_id, workspace_id, boundary_time, boundary_time, boundary_time, boundary_id, limit + 1)).fetchall()
        data = []
        for row in rows[:limit]:
            tasks = self.conversation_tasks(row["conversation_id"])
            data.append({**tasks[-1], "chatGroupId": row['family'], "title": self.conversation_title(row["conversation_id"], tasks), "lastActivityAt": row["activity"]})
        return {"data": data, "nextCursor": json.dumps([rows[limit-1]["activity"], rows[limit-1]["conversation_id"]]) if len(rows) > limit else None}

    def conversation_title(self, ident, tasks):
        row = self.db.execute("SELECT title FROM conversation_metadata WHERE conversation_id=?", (ident,)).fetchone()
        return row["title"] if row else tasks[0]["request"]["prompt"]

    def rename_conversation(self, ident, title):
        if not isinstance(title, str):
            raise ValueError("Chat title must be a string")
        title = title.strip()
        if not title or len(title) > 120 or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in title):
            raise ValueError("Chat title must be a single line of 1–120 characters")
        self.conversation_tasks(ident)
        from .reply_versions import members
        with self.db:
            for branch in members(self, ident):
                self.db.execute("INSERT INTO conversation_metadata(conversation_id,title) VALUES (?,?) "
                                "ON CONFLICT(conversation_id) DO UPDATE SET title=excluded.title", (branch, title))
        return {"id": ident, "title": title}

    def event_rows(self, ident, after=0, through=None, segment_id=None, limit=None):
        if self.segments and self.segments.ready(ident):
            with self.db:
                self.segments.sync_pending(ident)
            if segment_id is None and self.segments.scope and self.segments.scope[0] == ident:
                segment_id = self.segments.scope[1]
            return self.segments.rows(self.segments.info(ident, segment_id), after, through, limit)
        sql = 'SELECT e.* FROM events e JOIN tasks t ON e.task_id=t.id WHERE t.conversation_id=? AND e.id>? AND (? IS NULL OR e.id<=?) ORDER BY e.id'
        params = [ident, after, through, through]
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return self.db.execute(sql, params).fetchall()

    def conversation_events(self, ident, after=0, through=None, segment_id=None):
        return [{"id": row["id"], "taskId": row["task_id"], "kind": row["kind"],
                 "data": json.loads(row["data"]), "createdAt": row["created_at"]}
                for row in self.event_rows(ident, after, through, segment_id, 200)]

    def conversation_events_all(self, ident, segment_id=None):
        return [{'id': row['id'], 'taskId': row['task_id'], 'kind': row['kind'],
                 'data': json.loads(row['data']), 'createdAt': row['created_at']}
                for row in self.event_rows(ident, segment_id=segment_id)]

    def hold_queue(self, ident, held=True):
        with self.db:
            self.db.execute("INSERT INTO conversation_queues VALUES (?,?) ON CONFLICT(conversation_id) DO UPDATE SET held=excluded.held", (ident, int(held)))

    def task_context(self, ident):
        row = self.db.execute("SELECT workspace_context FROM tasks WHERE id=?", (ident,)).fetchone()
        if self.segments:
            payload = self.segments.task_payload(ident, self.segments.info(*self.segments.scope) if self.segments.scope else None)
            payload = payload or self.segments.task_payload(ident)
            if payload:
                return {**(json.loads(row[0]) if row else payload['context']),
                        **{key: value for key, value in payload['context'].items() if key.startswith('_')}}
        if row is None:
            raise KeyError(ident)
        return json.loads(row["workspace_context"])

    def history_seed(self, ident):
        if self.segments:
            payload = self.segments.task_payload(ident)
            if payload:
                return payload['seed']
        row = self.db.execute('SELECT history_seed FROM tasks WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return json.loads(row[0]) if row[0] is not None else None

    def set_history_seed(self, ident, history):
        payload = self.segments.task_payload(ident) if self.segments else None
        if payload:
            file_id = self.db.execute('SELECT file_id FROM task_segments WHERE task_id=?', (ident,)).fetchone()[0]
            db = self.segments.connection(file_id, True)
            with db:
                db.execute('UPDATE turns SET seed=? WHERE task_id=?', (json.dumps(history, ensure_ascii=False) if history is not None else None, ident))
            return
        with self.db:
            self.db.execute('UPDATE tasks SET history_seed=? WHERE id=?',
                            (json.dumps(history, ensure_ascii=False) if history is not None else None, ident))

    def workspace(self, ident):
        row = self.db.execute("SELECT * FROM workspaces WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return {"id": row["id"], "name": row["name"], "workingDirectory": row["directory"],
                "systemPrompt": row["system_prompt"], "skills": json.loads(row["skills"]),
                "browserMode": row["browser_mode"], **json.loads(row["browser_settings"]), "revision": row["revision"],
                "executionDetail": row["execution_detail"], "diagnosticLogLevel": row["diagnostic_log_level"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "taskCount": self.db.execute("SELECT count(DISTINCT coalesce((SELECT family_id FROM chat_families WHERE branch_id=conversation_id),conversation_id)) FROM tasks WHERE workspace_id=? AND conversation_id NOT IN (SELECT source_id FROM history_deletions)", (ident,)).fetchone()[0]}

    def workspaces(self):
        return {"data": [self.workspace(row["id"]) for row in self.db.execute("SELECT id FROM workspaces ORDER BY name COLLATE NOCASE,id")]}

    def save_workspace(self, body, ident=None):
        current = self.workspace(ident) if ident else None
        value = normalize_workspace(body, current, guest_paths=self.guest_paths)
        now = int(time.time())
        preferences = json.dumps({"browser": value["browser"], "browserProfile": value["browserProfile"]})
        if current:
            with self.db:
                self.db.execute("UPDATE workspaces SET name=?,directory=?,directory_key=?,system_prompt=?,skills=?,browser_mode=?,browser_settings=?,revision=revision+1,updated_at=? WHERE id=?",
                                (value["name"], value["workingDirectory"], directory_key(value["workingDirectory"], self.guest_paths), value["systemPrompt"], json.dumps(value["skills"]), value["browserMode"], preferences, now, ident))
        else:
            ident = str(uuid.uuid4())
            with self.db:
                self.db.execute("INSERT INTO workspaces (id,name,directory,directory_key,system_prompt,skills,browser_mode,browser_settings,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                                (ident, value["name"], value["workingDirectory"], directory_key(value["workingDirectory"], self.guest_paths), value["systemPrompt"], json.dumps(value["skills"]), value["browserMode"], preferences, now, now))
        with self.db:
            self.db.execute("UPDATE workspaces SET execution_detail=?,diagnostic_log_level=? WHERE id=?", (value["executionDetail"], value["diagnosticLogLevel"], ident))
        return self.workspace(ident)

    def workspace_for_directory(self, directory, browser="dedicated", name=None):
        key = directory_key(directory, self.guest_paths)
        row = self.db.execute("SELECT id FROM workspaces WHERE directory_key=? ORDER BY rowid LIMIT 1", (key,)).fetchone()
        if row:
            return self.workspace(row["id"])
        # Migration preserves historical directories even if they no longer exist.
        ident, now = str(uuid.uuid4()), int(time.time())
        display = PureWindowsPath(directory) if self.guest_paths else Path(directory).resolve()
        self.db.execute("INSERT INTO workspaces (id,name,directory,directory_key,system_prompt,skills,browser_mode,revision,created_at,updated_at) VALUES (?,?,?,?,?,'[]',?,1,?,?)",
                        (ident, name or display.name or str(display), str(display), key, "", browser, now, now))
        return self.workspace(ident)

    def close(self):
        if self.segments:
            self.segments.close()
        self.db.close()
