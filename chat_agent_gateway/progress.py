"""Read-only task and progress views shared by the local monitor and Lark."""

import fcntl
import sqlite3
from contextlib import closing

from .approval import IN_FLIGHT

MAX_TASKS = 12
MAX_EVENTS = 40
RESULT_PREVIEW_CHARS = 220

TASK_COLUMNS = """
id,status,is_blocked,substr(json_extract(request,'$.summary'),1,160)
  || CASE WHEN length(json_extract(request,'$.summary'))>160 THEN '…' ELSE '' END AS title,
(SELECT min(at) FROM run_event WHERE task_id=task.id AND kind='running') AS started,
(SELECT min(at) FROM run_event WHERE task_id=task.id AND kind='ready') AS ended
"""


def worker_online(state):
    try:
        with (state / "worker.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True
    return False


def snapshot(state, selected=None):
    online = worker_online(state)
    path = state / "approvals.sqlite3"
    info = dict(
        online=online,
        active=0,
        queued=0,
        interrupted=0,
        notices=0,
        tasks=[],
        events=[],
        result_preview="",
        total=0,
    )
    if not path.is_file():
        if online:
            raise FileNotFoundError(path)
        return info
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        counts = dict(db.execute("SELECT status,count(*) FROM task GROUP BY status"))
        info.update(
            active=counts.get("running", 0) if online else 0,
            queued=counts.get("approved", 0),
            interrupted=0
            if online
            else sum(counts.get(status, 0) for status in IN_FLIGHT),
            notices=counts.get("awaiting", 0) + counts.get("notified", 0),
        )
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_event'"
        ).fetchone():
            return info
        tasks = db.execute(
            f"""
            SELECT {TASK_COLUMNS}
            FROM task WHERE status IN ('approved','running','ready','sending','sent')
            ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'sending' THEN 1 WHEN 'ready' THEN 2
                     WHEN 'approved' THEN 3 ELSE 4 END,
                     CASE WHEN status='approved' THEN id END, id DESC LIMIT ?
        """,
            (MAX_TASKS,),
        ).fetchall()
        info["tasks"] = [dict(row) for row in tasks]
        focus = (
            selected
            if any(row["id"] == selected for row in tasks)
            else (tasks[0]["id"] if tasks else None)
        )
        if focus is not None:
            result, length = db.execute(
                "SELECT substr(result,1,?),length(result) FROM task WHERE id=?",
                (RESULT_PREVIEW_CHARS, focus),
            ).fetchone()
            if result:
                info["result_preview"] = " ".join(result.split())
                if length > RESULT_PREVIEW_CHARS:
                    info["result_preview"] += "…"
            info["total"] = db.execute(
                "SELECT count(*) FROM run_event WHERE task_id=?", (focus,)
            ).fetchone()[0]
            has_detail = "detail" in {
                row["name"] for row in db.execute("PRAGMA table_info(run_event)")
            }
            detail_column = "detail" if has_detail else "NULL AS detail"
            events = db.execute(
                f"SELECT at,label,{detail_column} FROM run_event WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (focus, MAX_EVENTS),
            ).fetchall()
            info["events"] = [
                (row["at"], row["label"], row["detail"]) for row in reversed(events)
            ]
    return info


def status_text(task, online):
    state = task["status"]
    if not online and state in ("running", "sending"):
        return "已中断，待核对"
    if task["is_blocked"] and state in ("ready", "sending", "sent"):
        return "需要核对"
    return {
        "approved": "排队中",
        "running": "处理中",
        "ready": "等待回传",
        "sending": "正在回传",
        "sent": "已回复",
    }[state]


def board_snapshot(path, limit):
    """Read the three sections from the same SQLite snapshot."""
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        counts = dict(db.execute("SELECT status,count(*) FROM task GROUP BY status"))
        current = db.execute(
            f"""
            SELECT {TASK_COLUMNS},
              (SELECT label FROM run_event WHERE task_id=task.id
               ORDER BY id DESC LIMIT 1) AS step
            FROM task WHERE status IN ('running','ready','sending')
            ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'sending' THEN 1 ELSE 2 END, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        waiting = db.execute(
            f"SELECT {TASK_COLUMNS} FROM task WHERE status='approved' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        recent = db.execute(
            f"""
            SELECT {TASK_COLUMNS} FROM task WHERE status='sent'
            ORDER BY (SELECT max(id) FROM run_event WHERE task_id=task.id AND kind='sent') DESC,
                     id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return (
            tuple(dict(row) for row in current),
            tuple(dict(row) for row in waiting),
            tuple(dict(row) for row in recent),
            sum(counts.get(status, 0) for status in ("running", "ready", "sending")),
            counts.get("approved", 0),
        )
