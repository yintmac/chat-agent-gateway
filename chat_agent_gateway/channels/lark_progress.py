"""Keep one task progress card in the owner's Lark private chat."""

import sqlite3
import sys
from secrets import token_hex
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import monotonic, time
from urllib.parse import urlencode

from ..notify import BridgeError
from ..progress import board_snapshot, status_text

REFRESH_SECONDS = 4
ELAPSED_REFRESH_SECONDS = 30
FAILED_REFRESH_SECONDS = 30
CARD_LIFETIME_SECONDS = 14 * 24 * 60 * 60
PIN_ROTATION_LEAD_SECONDS = 60 * 60
PIN_MARKER = "pin-progress"
ROWS_PER_SECTION = 5


class ProgressCardUnavailable(BridgeError):
    """Lark confirmed that the card is no longer editable."""


@dataclass(frozen=True)
class PostedCard:
    chat: str
    message: str
    position: int | None
    command: str
    created: int


def chat_link(chat, position=None):
    params = {"openChatId": chat}
    if position is not None and position >= 0:
        params["position"] = str(position)
    return "https://applink.larksuite.com/client/chat/open?" + urlencode(params)


def progress_card(view, stamp, chat):
    current, waiting, recent, active_count, queued_count = view

    def plain(text):
        return {"tag": "plain_text", "content": text}

    def section(heading, rows, *, count=None, live=False):
        lines = [heading]
        for task in rows:
            title = " ".join((task["title"] or "").split()) or "未命名任务"
            line = f"#{task['id']} {title}"
            if live:
                line += f" · {status_text(task, True)}"
                if task["started"] is not None:
                    end = task["ended"] if task["ended"] is not None else stamp
                    elapsed = max(0, end - task["started"])
                    line += f" · 已用 {elapsed // 60} 分 {elapsed % 60} 秒"
                if task["step"]:
                    line += " · " + task["step"][:160]
            elif count is None:
                line += " · " + ("受阻" if task["is_blocked"] else "已完成")
            lines.append(line)
        if not rows:
            lines.append("暂无")
        if count is not None and count > len(rows):
            lines.append(f"还有 {count - len(rows)} 个未展开")
        return {"tag": "div", "text": plain("\n".join(lines))}

    elements = [
        section(f"进行中 · {active_count}", current, count=active_count, live=True),
        section(f"排队中 · {queued_count}", waiting, count=queued_count),
        section(f"最近完成 · 最多 {ROWS_PER_SECTION} 个", recent),
    ]
    if chat:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": plain("打开原对话"),
                        "url": chat_link(chat),
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [
                plain(
                    "最后更新 "
                    + datetime.fromtimestamp(stamp, UTC)
                    .astimezone()
                    .strftime("%m-%d %H:%M:%S %Z")
                )
            ],
        }
    )
    return {
        "config": {"enable_forward": False, "update_multi": True},
        "header": {"template": "blue", "title": plain("任务进度")},
        "elements": elements,
    }


class ProgressBoard:
    def __init__(self, approvals, messages, *, pin=False):
        self.approvals = approvals
        self.messages = messages
        self.pin = pin
        self.pinned_message = None
        self.publish_pending = False
        self.lock = Lock()
        self.pending = None
        self.last_view = None
        self.last_updated = 0
        self.retry_after = 0.0
        with closing(approvals.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS lark_progress_card (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    chat TEXT NOT NULL,
                    message TEXT NOT NULL,
                    position INTEGER,
                    command TEXT NOT NULL,
                    created INTEGER NOT NULL
                )
            """)
            row = db.execute(
                "SELECT chat,message,position,command,created FROM lark_progress_card"
            ).fetchone()
            if pin:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS lark_progress_pin (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        message TEXT,
                        publish_pending INTEGER NOT NULL DEFAULT 0 CHECK (publish_pending IN (0, 1))
                    )
                """)
                db.execute(
                    "INSERT OR IGNORE INTO lark_progress_pin(singleton) VALUES (1)"
                )
                self.pinned_message, self.publish_pending = db.execute(
                    "SELECT message,publish_pending FROM lark_progress_pin"
                ).fetchone()
        self.post = PostedCard(*row) if row else None
        if self.publish_pending:
            print(
                "Lark 置顶看板上次发布结果未确认，需先核对私聊。",
                file=sys.stderr,
                flush=True,
            )

    def request(self, chat, message):
        with self.lock:
            self.pending = (chat, message)

    def save(self, post, *, published=False):
        with closing(self.approvals.connect()) as db, db:
            if post is None:
                db.execute("DELETE FROM lark_progress_card WHERE singleton=1")
            else:
                db.execute(
                    "INSERT OR REPLACE INTO lark_progress_card VALUES (1,?,?,?,?,?)",
                    (
                        post.chat,
                        post.message,
                        post.position,
                        post.command,
                        post.created,
                    ),
                )
            if published:
                db.execute(
                    "UPDATE lark_progress_pin SET publish_pending=0 WHERE singleton=1"
                )
        self.post = post
        if published:
            self.publish_pending = False

    def publish_pinned(self, view, stamp, previous):
        # Persist the intent before sending. An ambiguous create must not post another card.
        with closing(self.approvals.connect()) as db, db:
            db.execute(
                "UPDATE lark_progress_pin SET publish_pending=1 WHERE singleton=1"
            )
        self.publish_pending = True
        chat = previous.chat if previous else None
        card = progress_card(view, stamp, chat)
        try:
            chat, message, position = self.messages.send_owner_progress_card(
                card, uuid=token_hex(16)
            )
        except BridgeError as error:
            raise BridgeError(f"{error}，需先核对私聊中的看板投递") from None
        post = PostedCard(chat, message, position, "", stamp)
        self.save(post, published=True)
        self.last_updated = stamp
        return post

    def sync_pin(self, post):
        self.messages.pin_progress_card(post.message)
        if self.pinned_message:
            self.messages.unpin_progress_card(self.pinned_message)
        with closing(self.approvals.connect()) as db, db:
            db.execute(
                "UPDATE lark_progress_pin SET message=? WHERE singleton=1",
                (post.message,),
            )
        self.pinned_message = post.message
        print("Lark 任务进度卡片已置顶。", flush=True)

    def refresh(self):
        with self.lock:
            request, self.pending = self.pending, None
        if request is None:
            should_publish = self.pin and not self.publish_pending
            if (
                self.post is None and not should_publish
            ) or monotonic() < self.retry_after:
                return

        stamp = int(time())
        try:
            self.refresh_card(request, stamp)
        except (BridgeError, sqlite3.Error) as error:
            detail = (
                str(error) if isinstance(error, BridgeError) else type(error).__name__
            )
            print(f"Lark 进度看板更新失败（{detail}）。", file=sys.stderr, flush=True)
            self.retry_after = monotonic() + FAILED_REFRESH_SECONDS
            if request:
                try:
                    self.messages.reply(
                        request[1],
                        "这次没能刷新进度看板，请稍后再发「进度」。",
                        uuid=f"{request[1]}-progress-error",
                    )
                except BridgeError:
                    print(
                        "Lark 进度看板错误提示投递失败。", file=sys.stderr, flush=True
                    )
        else:
            self.retry_after = 0.0

    def refresh_card(self, request, stamp):
        post = self.post
        if post and stamp - post.created >= CARD_LIFETIME_SECONDS:
            self.save(None)
            post = None
        if post is None and request is None and not self.pin:
            return
        if post and request and post.chat != request[0]:
            raise BridgeError("进度命令与当前看板的私聊不一致")

        view = board_snapshot(self.approvals.path, ROWS_PER_SECTION)
        refresh_elapsed = stamp - self.last_updated >= ELAPSED_REFRESH_SECONDS and any(
            task["status"] == "running" for task in view[0]
        )
        if post and (request is not None or view != self.last_view or refresh_elapsed):
            try:
                self.messages.update_card(
                    post.message, progress_card(view, stamp, post.chat)
                )
            except ProgressCardUnavailable:
                self.save(None)
                post = None
            else:
                self.last_updated = stamp

        published = False
        if self.pin:
            needs_rotation = (
                post is not None
                and stamp - post.created
                >= CARD_LIFETIME_SECONDS - PIN_ROTATION_LEAD_SECONDS
                and self.pinned_message == post.message
            )
            if (post is None or needs_rotation) and not self.publish_pending:
                post = self.publish_pinned(view, stamp, post)
                published = True
            if post is not None and self.pinned_message != post.message:
                self.sync_pin(post)

        if request is not None:
            chat, command = request
            if post is None:
                if self.pin and self.publish_pending:
                    raise BridgeError("上次看板投递结果未确认，请核对私聊")
                message, position = self.messages.send_progress_card(
                    chat, progress_card(view, stamp, chat), uuid=f"{command}-progress"
                )
                self.save(PostedCard(chat, message, position, command, stamp))
                self.last_updated = stamp
            elif post.command != command:
                self.messages.reply(
                    command,
                    "打开任务进度看板\n" + chat_link(post.chat, post.position),
                    uuid=f"{command}-progress-link",
                )
        self.last_view = None if published else view
