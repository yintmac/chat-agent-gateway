"""Persistent approval and delivery state for the Mattermost pilot."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

IN_FLIGHT = ("posting", "running", "sending")


@dataclass(frozen=True)
class Request:
    event_id: str
    topic: str
    source: str
    summary: str
    channel_id: str
    root_id: str
    update: str = ""


@dataclass(frozen=True)
class Reply:
    user_id: str
    channel_id: str
    root_id: str
    message: str


class Approvals:
    def __init__(self, path: Path, owner_id: str, inbox_id: str):
        self.path = path
        self.owner_id = owner_id
        self.inbox_id = inbox_id
        with closing(self.connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    user_id TEXT NOT NULL,
                    inbox_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task (
                    id INTEGER PRIMARY KEY,
                    topic TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_post_id TEXT UNIQUE,
                    result TEXT,
                    is_blocked INTEGER NOT NULL DEFAULT 0,
                    result_post_id TEXT
                );
                CREATE TABLE IF NOT EXISTS event (
                    id TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES task(id)
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO owner VALUES (1, ?, ?)", (owner_id, inbox_id)
            )
            if db.execute("SELECT user_id, inbox_id FROM owner").fetchone() != (
                owner_id,
                inbox_id,
            ):
                raise ValueError("Approval database belongs to another inbox")

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def enqueue(
        self, request: Request, *, related_event_id: Optional[str] = None
    ) -> int:
        """Deduplicate events and identical requests without widening approval scope."""
        payload = asdict(request)
        del payload["event_id"]
        if not request.update:
            del payload["update"]
        serialized = json.dumps(payload, sort_keys=True)
        event_ids = {request.event_id}
        if related_event_id:
            event_ids.add(related_event_id)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT DISTINCT task_id FROM event WHERE id IN (?, ?)",
                (request.event_id, related_event_id),
            ).fetchall()
            if len(existing) > 1:
                raise ValueError(
                    "Related events are already bound to different approvals"
                )
            if existing:
                task_id = existing[0][0]
                db.executemany(
                    "INSERT OR IGNORE INTO event VALUES (?, ?)",
                    ((event_id, task_id) for event_id in event_ids),
                )
                return task_id

            matching = db.execute(
                """
                SELECT id FROM task WHERE topic = ?
                AND json_remove(request, '$.workspace', '$.recipient', '$.suggested_workspace', '$.auto_route', '$.update')
                    = json_remove(?, '$.update')
                AND status IN ('queued', 'posting', 'awaiting', 'approved', 'running', 'ready', 'sending')
                ORDER BY id DESC LIMIT 1
                """,
                (request.topic, serialized),
            ).fetchone()
            if matching is not None:
                task_id = matching[0]
            else:
                task_id = db.execute(
                    "INSERT INTO task (topic, request, status) VALUES (?, ?, 'queued')",
                    (request.topic, serialized),
                ).lastrowid
            db.executemany(
                "INSERT INTO event VALUES (?, ?)",
                ((event_id, task_id) for event_id in event_ids),
            )
            return task_id

    def start_approval(self, task_id: int):
        with closing(self.connect()) as db, db:
            row = db.execute(
                """
                UPDATE task SET status = 'posting' WHERE id = ? AND status = 'queued'
                RETURNING request
                """,
                (task_id,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def approval_posted(self, task_id: int, post_id: str, *, notice=False):
        if not post_id:
            raise ValueError("Mattermost approval post ID is required")
        with closing(self.connect()) as db, db:
            return (
                db.execute(
                    """
                UPDATE task SET status = ?, approval_post_id = ?
                WHERE id = ? AND status = 'posting'
                """,
                    ("notified" if notice else "awaiting", post_id, task_id),
                ).rowcount
                == 1
            )

    def record_approval_scope(
        self, task_id: int, workspace: Optional[str], *, auto_route: bool
    ):
        with closing(self.connect()) as db, db:
            changed = db.execute(
                """
                UPDATE task SET request = json_set(request, '$.suggested_workspace', ?, '$.auto_route', ?)
                WHERE id = ? AND status = 'posting'
                """,
                (workspace, auto_route, task_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Approval is no longer being prepared for delivery")

    def accept_reply(
        self,
        reply: Reply,
        *,
        workspace: Optional[str] = None,
        recipient: Optional[str] = None,
    ):
        if (
            reply.user_id != self.owner_id
            or reply.channel_id != self.inbox_id
            or not reply.root_id
        ):
            return None
        parts = reply.message.split()
        if not parts or parts[0] not in {"确认", "取消"}:
            return None
        if parts[0] == "取消" and (len(parts) != 1 or workspace or recipient):
            return None
        if parts[0] == "确认" and len(parts) != 1 + bool(workspace) + bool(recipient):
            return None
        status = "approved" if parts[0] == "确认" else "cancelled"
        with closing(self.connect()) as db, db:
            row = db.execute(
                """
                UPDATE task SET status = ?,
                    request = json_set(request, '$.workspace',
                                       coalesce(?, json_extract(request, '$.suggested_workspace')),
                                       '$.recipient', ?)
                WHERE approval_post_id = ? AND status = 'awaiting'
                RETURNING id
                """,
                (status, workspace, recipient, reply.root_id),
            ).fetchone()
            return row[0] if row else None

    def tasks_in(self, status: str):
        with closing(self.connect()) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM task WHERE status = ? ORDER BY id", (status,)
                )
            ]

    def is_awaiting(self, post_id: str):
        with closing(self.connect()) as db:
            return (
                db.execute(
                    "SELECT 1 FROM task WHERE approval_post_id = ? AND status = 'awaiting'",
                    (post_id,),
                ).fetchone()
                is not None
            )

    def awaiting_post_ids(self):
        with closing(self.connect()) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT approval_post_id FROM task WHERE status = 'awaiting' ORDER BY id"
                )
            ]

    def awaiting_post_for_task(self, task_id: int):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT approval_post_id FROM task WHERE id = ? AND status = 'awaiting'",
                (task_id,),
            ).fetchone()
            return row[0] if row else None

    def claim(self, task_id: int):
        with closing(self.connect()) as db, db:
            row = db.execute(
                """
                UPDATE task SET status = 'running' WHERE id = ? AND status = 'approved'
                RETURNING request
                """,
                (task_id,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def finish(self, task_id: int, result: str, *, blocked: bool):
        if not result.strip():
            raise ValueError("A result or blocker is required")
        with closing(self.connect()) as db, db:
            return (
                db.execute(
                    """
                UPDATE task SET status = 'ready', result = ?, is_blocked = ?
                WHERE id = ? AND status = 'running'
                """,
                    (result, blocked, task_id),
                ).rowcount
                == 1
            )

    def start_result(self, task_id: int):
        with closing(self.connect()) as db, db:
            row = db.execute(
                """
                UPDATE task SET status = 'sending'
                WHERE id = ? AND status = 'ready'
                RETURNING request, result, is_blocked, approval_post_id
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            payload, result, blocked, approval_post_id = row
            if blocked:
                return self.inbox_id, approval_post_id, None, result, None
            request = json.loads(payload)
            return (
                request["channel_id"] or self.inbox_id,
                request["root_id"] or approval_post_id,
                request["recipient"],
                result,
                request["topic"],
            )

    def result_posted(self, task_id: int, post_id: str):
        if not post_id:
            raise ValueError("Mattermost result post ID is required")
        with closing(self.connect()) as db, db:
            return (
                db.execute(
                    """
                UPDATE task SET status = 'sent', result_post_id = ?
                WHERE id = ? AND status = 'sending'
                """,
                    (post_id, task_id),
                ).rowcount
                == 1
            )
