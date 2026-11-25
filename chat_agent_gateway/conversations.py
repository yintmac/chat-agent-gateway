"""Durable, owner-initiated chat turns and their observable execution events."""

import json
from contextlib import closing


class Conversations:
    def __init__(self, approvals):
        self.approvals = approvals
        with closing(approvals.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dialog (
                    id INTEGER PRIMARY KEY,
                    chat TEXT NOT NULL,
                    notice INTEGER REFERENCES task(id),
                    UNIQUE(chat, notice)
                );
                CREATE TABLE IF NOT EXISTS dialog_focus (
                    chat TEXT PRIMARY KEY, dialog INTEGER NOT NULL REFERENCES dialog(id)
                );
                CREATE TABLE IF NOT EXISTS chat_turn (
                    task_id INTEGER PRIMARY KEY REFERENCES task(id),
                    dialog INTEGER REFERENCES dialog(id),
                    message TEXT UNIQUE NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turn_by_dialog ON chat_turn(dialog, task_id);
                CREATE TABLE IF NOT EXISTS run_event (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES task(id),
                    at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    kind TEXT NOT NULL, label TEXT NOT NULL, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS events_by_task ON run_event(task_id, id);
                CREATE TRIGGER IF NOT EXISTS record_run_transition
                AFTER UPDATE OF status ON task
                WHEN NEW.status <> OLD.status AND NEW.status IN ('approved','running','ready','sending','sent')
                BEGIN
                    INSERT INTO run_event(task_id,kind,label) VALUES (NEW.id, NEW.status,
                        CASE NEW.status WHEN 'approved' THEN '已进入队列'
                            WHEN 'running' THEN '开始处理'
                            WHEN 'ready' THEN CASE NEW.is_blocked WHEN 1 THEN '需要你核对' ELSE '回复已生成' END
                            WHEN 'sending' THEN '正在发送回复' WHEN 'sent' THEN '回复已送达' END);
                END;
            """)
            db.execute("BEGIN IMMEDIATE")
            if "detail" not in {
                row[1] for row in db.execute("PRAGMA table_info(run_event)")
            }:
                db.execute("ALTER TABLE run_event ADD COLUMN detail TEXT")

    def receive(self, chat, message, text, references):
        """Select the conversation and enqueue exactly once in the same transaction."""
        text = text.strip()
        if not text or not chat or not message:
            return None
        fresh = text.split(maxsplit=1)[0] == "新对话"
        instruction = text.removeprefix("新对话").lstrip() if fresh else text
        with closing(self.approvals.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT task_id FROM chat_turn WHERE message=?", (message,)
            ).fetchone()
            if existing:
                return existing[0]
            dialog = None
            note = None
            if references and not fresh:
                dialog = self._quoted_dialog(db, chat, references)
                if dialog is None:
                    note = "找不到这条引用的上下文。请回复机器人的工单提醒，或在私聊中直接发完整需求。"
            elif not fresh:
                focus = db.execute(
                    "SELECT dialog FROM dialog_focus WHERE chat=?", (chat,)
                ).fetchone()
                dialog = focus[0] if focus else None
            if note is None:
                if dialog is None:
                    dialog = db.execute(
                        "INSERT INTO dialog(chat) VALUES (?)", (chat,)
                    ).lastrowid
                db.execute(
                    "INSERT INTO dialog_focus VALUES (?,?) ON CONFLICT(chat) DO UPDATE SET dialog=excluded.dialog",
                    (chat, dialog),
                )
                if not instruction:
                    note = "已开启新对话。把你的需求发给我。"

            source = db.execute(
                """
                SELECT t.request,t.result FROM dialog d JOIN task t ON t.id=d.notice WHERE d.id=?
            """,
                (dialog,),
            ).fetchone()
            context = json.loads(source[0]) if source else {}
            reference = context.get("summary", "")
            if context.get("update") and context["update"] not in reference:
                reference += "\n\n" + context["update"]
            if source and source[1]:
                reference += "\n\n此前的回复：\n" + source[1]
            request = {
                "source": context.get("source", "私聊"),
                "topic": context.get("topic", "chat"),
                "summary": instruction or text,
                "reference": reference,
                "conversation": True,
                "workspace": None,
                "auto_route": True,
                "recipient": None,
                "channel_id": chat,
                "root_id": message,
            }
            blocked = dialog is None
            status, label = "approved", "收到消息，已进入队列"
            if note:
                status, label = "ready", "需要你核对" if blocked else "回复已生成"
            task = db.execute(
                """
                INSERT INTO task(topic,request,status,result,is_blocked) VALUES(?,?,?,?,?)
            """,
                (
                    request["topic"],
                    json.dumps(request, ensure_ascii=False),
                    status,
                    note,
                    int(blocked),
                ),
            ).lastrowid
            db.execute("INSERT INTO chat_turn VALUES(?,?,?)", (task, dialog, message))
            db.execute(
                "INSERT INTO run_event(task_id,kind,label) VALUES(?,?,?)",
                (task, status, label),
            )
            return task

    @staticmethod
    def _quoted_dialog(db, chat, references):
        for ref in dict.fromkeys(references):
            row = db.execute(
                """
                SELECT c.dialog FROM chat_turn c JOIN dialog d ON d.id=c.dialog
                JOIN task t ON t.id=c.task_id
                WHERE d.chat=? AND (c.message=? OR t.result_post_id=?)
            """,
                (chat, ref, ref),
            ).fetchone()
            if row:
                return row[0]
            notice = db.execute(
                """
                SELECT id FROM task WHERE (approval_post_id=? OR result_post_id=?)
                AND NOT EXISTS (SELECT 1 FROM chat_turn WHERE task_id=task.id)
            """,
                (ref, ref),
            ).fetchone()
            if notice:
                db.execute(
                    "INSERT INTO dialog(chat,notice) VALUES(?,?) ON CONFLICT(chat,notice) DO NOTHING",
                    (chat, notice[0]),
                )
                # An existing approval card can also be answered conversationally.
                db.execute(
                    "UPDATE task SET status='discussed' WHERE id=? AND status IN ('awaiting','notified')",
                    notice,
                )
                return db.execute(
                    "SELECT id FROM dialog WHERE chat=? AND notice=?", (chat, notice[0])
                ).fetchone()[0]
        return None

    def history(self, task):
        """Stream all previous turns, releasing the read snapshot between pages."""
        cursor = 0
        while True:
            with closing(self.approvals.connect()) as db:
                rows = db.execute(
                    """
                    SELECT t.id,json_extract(t.request,'$.summary'),t.result FROM chat_turn c
                    JOIN task t ON t.id=c.task_id
                    WHERE c.dialog=(SELECT dialog FROM chat_turn WHERE task_id=?)
                      AND c.task_id>? AND c.task_id<? AND t.result IS NOT NULL
                    ORDER BY c.task_id LIMIT 32
                """,
                    (task, cursor, task),
                ).fetchall()
            if not rows:
                return
            for cursor, instruction, answer in rows:
                yield {"user": instruction, "assistant": answer}

    def reply_target(self, task):
        with closing(self.approvals.connect()) as db:
            row = db.execute(
                "SELECT message FROM chat_turn WHERE task_id=?", (task,)
            ).fetchone()
        return row[0] if row else None

    def progress(self, task, label, *, detail=None):
        with closing(self.approvals.connect()) as db, db:
            db.execute(
                """INSERT INTO run_event(task_id,kind,label,detail)
                          SELECT id,'step',?,? FROM task WHERE id=? AND status='running'""",
                (label, detail, task),
            )
