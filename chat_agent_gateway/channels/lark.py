"""Receive owner conversations and deliver external notices in a private Lark chat."""

import asyncio
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from getpass import GetPassWarning, getpass
from pathlib import Path
from threading import Event, Lock, Thread
from warnings import catch_warnings, simplefilter

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreatePinRequest,
    CreatePinRequestBody,
    DeletePinRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from ..approval import IN_FLIGHT, Approvals, Reply
from ..config import SETTINGS
from ..conversations import Conversations
from ..local import acquire_lock, require_private
from ..notify import (
    MATTERMOST,
    OPENPROJECT,
    OPENPROJECT_USER_ID,
    BridgeError,
    openproject_client,
    write_state,
)
from ..openproject_watch import assigned_open_ticket_report, ticket_summary
from ..paths import STATE_DIR as MATTERMOST_STATE
from ..workflow import ApprovalWorkflow, RunJob
from .lark_progress import (
    PIN_MARKER,
    REFRESH_SECONDS,
    ProgressBoard,
    ProgressCardUnavailable,
)
from .mattermost_watch import enable_mattermost, watch_mattermost

STATE_DIR = MATTERMOST_STATE / "lark"
OPEN_ID = re.compile(r"ou_[a-zA-Z0-9_-]+")
APP_ID = re.compile(r"cli_[a-zA-Z0-9]+")
INBOX = "lark-private-chat"
APPROVAL_ACTIONS = {"确认", "取消"}
RECIPIENT_COMMAND = re.compile(
    r"确认\s+审批([1-9][0-9]{0,18})\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
)
OP_SITE = r"(?:openproject|op)(?:上)?"
OP_OWNER = r"(?:我名下的?|我的|分配给我的|我)"
OP_UNCLOSED = r"(?:目前|当前)?(?:还?有)?(?:哪些|什么|所有)?(?:还?没有?关闭|未关闭|没关|未关)(?:的)?"
OP_TICKET = r"(?:工单|tickets?)"
OPEN_TICKETS_QUERY = re.compile(
    rf"""
    (?:请|麻烦)?(?:帮我)?(?:查询|查(?:一下|下)?|看(?:一下|下)?|列出|列一下)?
    (?:
        (?:{OP_SITE}(?:有)?{OP_OWNER}|我在{OP_SITE}|{OP_OWNER}{OP_SITE}){OP_UNCLOSED}{OP_TICKET}
        | {OP_OWNER}{OP_UNCLOSED}{OP_SITE}{OP_TICKET}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Lark's text message limit.
LARK_TEXT_LIMIT_BYTES = 150 * 1024


def reply_text(text):
    return re.sub(r"<(?=/?at(?:\s|>))", "＜", text, flags=re.IGNORECASE)


def text_reply_body(text, uuid=None):
    return (
        ReplyMessageRequestBody.builder()
        .msg_type("text")
        .content(json.dumps({"text": text}, ensure_ascii=False))
        .uuid(uuid)
        .build()
    )


def read_config(path):
    try:
        with path.open(encoding="utf-8") as file:
            require_private(path, file, exact_mode=0o600)
            config = json.load(file)
    except FileNotFoundError:
        raise BridgeError("请先运行 pair，绑定你的 Lark 账号。") from None
    except ValueError:
        raise BridgeError("Lark 凭据文件格式有误。") from None
    if (
        not isinstance(config, dict)
        or not APP_ID.fullmatch(str(config.get("app_id", "")))
        or not isinstance(config.get("app_secret"), str)
        or not config["app_secret"]
        or not OPEN_ID.fullmatch(str(config.get("owner", "")))
    ):
        raise BridgeError("Lark 凭据文件格式有误。")
    return config


def dispatcher():
    return lark.EventDispatcherHandler.builder("", "")


@contextmanager
def long_connection(config, handler):
    client = lark.ws.Client(
        config["app_id"],
        config["app_secret"],
        domain=lark.LARK_DOMAIN,
        event_handler=handler,
        log_level=lark.LogLevel.CRITICAL,
    )
    exited = Event()

    def connect():
        try:
            client.start()
        except Exception as error:
            print(f"Lark 长连接停止（{type(error).__name__}）。", file=sys.stderr)
        finally:
            exited.set()

    Thread(target=connect, name="lark-connection", daemon=True).start()
    yield exited


def wait_for_event(event, exited):
    while not event.wait(0.5):
        if exited.is_set():
            raise BridgeError("Lark 长连接已停止，请核对应用凭据和长连接配置。")


def private_text(data):
    if not data.event or not data.event.message or not data.event.sender:
        return None
    message = data.event.message
    sender = data.event.sender
    if (
        message.chat_type != "p2p"
        or message.message_type != "text"
        or sender.sender_type != "user"
        or not sender.sender_id
        or not sender.sender_id.open_id
    ):
        return None
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("text"), str)
        and payload["text"].strip()
    ):
        return sender.sender_id.open_id, payload["text"]
    return None


def pair(path):
    if path.exists():
        raise BridgeError("这台电脑已绑定 Lark 应用。")
    if not sys.stdin.isatty():
        raise BridgeError("请在终端运行 pair。")
    app_id = input("Lark App ID: ").strip()
    with catch_warnings():
        simplefilter("error", GetPassWarning)
        try:
            app_secret = getpass("Lark App Secret: ").strip()
        except GetPassWarning:
            raise BridgeError("终端无法隐藏 App Secret。") from None
    if not APP_ID.fullmatch(app_id) or not app_secret:
        raise BridgeError("Lark App ID 或 App Secret 无效。")
    config = {"app_id": app_id, "app_secret": app_secret}
    code = f"绑定 {secrets.token_urlsafe(12)}"
    paired = Event()

    def receive(data):
        if paired.is_set():
            return
        message = private_text(data)
        if (
            message
            and message[0]
            and OPEN_ID.fullmatch(message[0])
            and message[1] == code
        ):
            owner, _ = message
            config["owner"] = owner
            paired.set()

    handler = dispatcher().register_p2_im_message_receive_v1(receive).build()
    with long_connection(config, handler) as exited:
        print("连接启动后，在 Lark 开发者后台保存长连接配置并发布应用。", flush=True)
        print(f"私聊机器人发送「{code}」（不含引号）。", flush=True)
        wait_for_event(paired, exited)
    write_state(path, config)
    print("Lark 已绑定。", flush=True)


class LarkMessages:
    def __init__(self, config):
        self.owner = config["owner"]
        self.client = (
            lark.Client.builder()
            .app_id(config["app_id"])
            .app_secret(config["app_secret"])
            .domain(lark.LARK_DOMAIN)
            .log_level(lark.LogLevel.CRITICAL)
            .timeout(15)
            .build()
        )

    def send(self, kind, content, *, recipient=None):
        return self._create(
            "email" if recipient else "open_id", recipient or self.owner, kind, content
        ).message_id

    def send_progress_card(self, chat, card, *, uuid):
        message = self._create("chat_id", chat, "interactive", card, uuid=uuid)
        return message.message_id, message.message_position

    def send_owner_progress_card(self, card, *, uuid):
        message = self._create("open_id", self.owner, "interactive", card, uuid=uuid)
        if not message.chat_id:
            raise BridgeError("Lark 看板已发送，但没有返回私聊 ID")
        return message.chat_id, message.message_id, message.message_position

    def _create(self, id_type, recipient, kind, content, *, uuid=None):
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(recipient)
                .msg_type(kind)
                .content(json.dumps(content, ensure_ascii=False))
                .uuid(uuid)
                .build()
            )
            .build()
        )
        return self._send(self.client.im.v1.message.create, request)

    def reply(self, message_id, text, *, uuid=None):
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(text_reply_body(text, uuid))
            .build()
        )
        return self._send(self.client.im.v1.message.reply, request).message_id

    def update_card(self, message_id, card):
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        try:
            response = self.client.im.v1.message.patch(request)
        except Exception as error:
            raise BridgeError(
                f"Lark 卡片更新请求失败（{type(error).__name__}）"
            ) from None
        if response.code in (230031, 230110):  # Expired or deleted.
            raise ProgressCardUnavailable("Lark 看板已过期或删除")
        if not response.success():
            raise BridgeError(f"Lark 卡片更新失败（代码 {response.code}）")

    def pin_progress_card(self, message_id):
        request = (
            CreatePinRequest.builder()
            .request_body(CreatePinRequestBody.builder().message_id(message_id).build())
            .build()
        )
        try:
            response = self.client.im.v1.pin.create(request)
        except Exception as error:
            raise BridgeError(f"Lark 置顶请求失败（{type(error).__name__}）") from None
        if not response.success():
            raise BridgeError(f"Lark 置顶失败（代码 {response.code}）")
        if (
            not response.data
            or not response.data.pin
            or response.data.pin.message_id != message_id
        ):
            raise BridgeError("Lark 未返回指定看板的置顶记录")

    def unpin_progress_card(self, message_id):
        request = DeletePinRequest.builder().message_id(message_id).build()
        try:
            response = self.client.im.v1.pin.delete(request)
        except Exception as error:
            raise BridgeError(
                f"Lark 取消旧看板置顶请求失败（{type(error).__name__}）"
            ) from None
        if not response.success() and response.code not in (230011, 230110):
            raise BridgeError(f"Lark 取消旧看板置顶失败（代码 {response.code}）")

    @staticmethod
    def _send(operation, request):
        try:
            response = operation(request)
        except Exception as error:
            raise BridgeError(f"Lark 发送失败（{type(error).__name__}）。") from None
        if not response.success() or not response.data or not response.data.message_id:
            raise BridgeError(f"Lark 发送未得到消息 ID（代码 {response.code}）。")
        return response.data


def plain(text):
    return {"tag": "plain_text", "content": text}


def approval_card(
    title,
    summary,
    *,
    source=None,
    diagnostic=None,
    details=None,
    from_mattermost=False,
    notice=False,
):
    actions = [
        {
            "tag": "button",
            "text": plain(action),
            "type": "primary" if action == "确认" else "default",
            "value": {"decision": action, "diagnostic": diagnostic or ""},
        }
        for action in (() if notice else ("确认", "取消"))
    ]
    if source:
        link = "看原消息" if from_mattermost else "看工单"
        actions.append({"tag": "button", "text": plain(link), "url": source})
    elements = [{"tag": "div", "text": plain(summary)}]
    if details:
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": plain("消息详情" if from_mattermost else "工单详情"),
                    "icon": {
                        "tag": "standard_icon",
                        "token": "down-small-ccm_outlined",
                    },
                    "icon_position": "follow_text",
                },
                "elements": [{"tag": "div", "text": plain(details)}],
            }
        )
    if notice:
        elements.append(
            {"tag": "note", "elements": [plain("回复这条提醒，告诉我你想怎么处理。")]}
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"enable_forward": False},
        "header": {"title": plain(title)},
        "elements": elements,
    }


def ticket_card(task, request):
    from_mattermost = bool(
        MATTERMOST and request["source"].startswith(f"{MATTERMOST}/_redirect/pl/")
    )
    if from_mattermost:
        title = "Mattermost @你"
        if request["topic"].startswith("ticket:"):
            title += f" · #{request['topic'].removeprefix('ticket:')}"
        update = body = request["summary"]
    else:
        title, _, body = request["summary"].partition("\n\n")
        update = request.get("update", "")
    lines = [line.strip() for line in update.splitlines() if line.strip()]
    preview = "\n".join(lines[:3])
    if len(lines) > 3 or len(preview) > 120:
        preview = preview[:119].rstrip() + "…"
    if preview:
        title = f"{title}\n\n{preview}"
        if body == preview:
            body = ""
    elif len(body) <= 120 and len(body.splitlines()) <= 3:
        if body:
            title = f"{title}\n\n{body}"
        body = ""
    if len(body) > 2000:
        link = "原消息" if from_mattermost else "工单"
        body = body[:2000].rstrip() + f"\n\n剩余内容见{link}。"
    return approval_card(
        f"提醒 {task}",
        title,
        source=request["source"],
        details=body,
        from_mattermost=from_mattermost,
        notice=True,
    )


def action_data(data, owner):
    event = data.event
    if not event or not event.operator or event.operator.open_id != owner:
        return None
    if not event.context or not event.context.open_message_id or not event.action:
        return None
    values = event.action.value or {}
    action = values.get("decision")
    if not isinstance(action, str) or action not in APPROVAL_ACTIONS:
        return None
    return event.context.open_message_id, action, values.get("diagnostic", "")


def toast(text, kind="success"):
    return P2CardActionTriggerResponse({"toast": {"type": kind, "content": text}})


def check(config, messages):
    verified = Event()
    diagnostic = secrets.token_urlsafe(16)

    def receive(data):
        details = action_data(data, messages.owner)
        if details and details[2] == diagnostic:
            verified.set()
            return toast("按钮回调成功。")
        return toast("这张卡不属于本次检查。", "warning")

    handler = dispatcher().register_p2_card_action_trigger(receive).build()
    with long_connection(config, handler) as exited:
        messages.send(
            "interactive",
            approval_card(
                "审批按钮测试",
                "随便点一个按钮。这张卡只测连接。",
                diagnostic=diagnostic,
            ),
        )
        print("测试卡已发到机器人私聊，请点「确认」或「取消」。", flush=True)
        wait_for_event(verified, exited)
        print("Lark 发卡和按钮回调都已通过。", flush=True)
        if sys.stdin.isatty():
            input("看到 Lark 的提示后，按回车结束测试。")


class LarkChannel(ApprovalWorkflow):
    def __init__(
        self,
        op,
        messages,
        approvals,
        cursor_path,
        *,
        run_job: RunJob,
        pin_progress=False,
    ):
        super().__init__(op, approvals, cursor_path, run_job)
        self.messages = messages
        self.dialogs = Conversations(approvals)
        self.board = ProgressBoard(approvals, messages, pin=pin_progress)
        self.delivery_lock = Lock()

    async def run_task(self, task_id, request):
        def progress(label, *, detail=None):
            self.dialogs.progress(task_id, label, detail=detail)

        if request.get("conversation"):
            instruction = "".join(request["summary"].split()).rstrip("？?。！!")
            if OPEN_TICKETS_QUERY.fullmatch(instruction):
                if self.op is None:
                    raise BridgeError("请先配置 OpenProject，再查询名下工单。")
                progress("查询 OpenProject 未关闭工单", detail="范围为分配给你的工单")
                result, count = await asyncio.to_thread(
                    assigned_open_ticket_report, self.op
                )
                body = text_reply_body(reply_text(result))
                if len(lark.JSON.marshal(body).encode("utf-8")) > LARK_TEXT_LIMIT_BYTES:
                    raise BridgeError(
                        f"OpenProject 查到 {count} 条未关闭工单，完整列表超过 Lark 单条文本上限。"
                    )
                progress("OpenProject 查询完成", detail=f"共 {count} 条未关闭工单")
                return result, False
            request["history"] = lambda: self.dialogs.history(task_id)
        if MATTERMOST and request["source"].startswith(f"{MATTERMOST}/_redirect/pl/"):
            ticket_id = request["topic"].removeprefix("ticket:")
            if ticket_id.isdecimal():
                if self.op is None:
                    raise BridgeError("请先配置 OpenProject，再读取关联工单。")
                progress("读取关联工单", detail=f"OpenProject 工单 #{ticket_id}")
                ticket = await asyncio.to_thread(
                    self.op.request, f"/api/v3/work_packages/{ticket_id}"
                )
                if ticket["id"] != int(ticket_id):
                    raise BridgeError("OpenProject 返回的工单与 Mattermost 消息不符。")
                key = "reference" if request.get("conversation") else "summary"
                request[key] += "\n\n" + ticket_summary(ticket)
        return await self.run_job(request, progress=progress)

    def receive(self, data):
        details = action_data(data, self.approvals.owner_id)
        if not details or details[2]:
            return toast("这张卡无法在当前会话中审批。", "warning")
        post_id, action, _ = details
        task = self.approvals.accept_reply(
            Reply(self.approvals.owner_id, self.approvals.inbox_id, post_id, action)
        )
        if task is None:
            return toast("这张卡已处理，或还在发送。可以刷新后核对。", "warning")
        print(f"收到「{action}」，审批{task}。", flush=True)
        return toast("已确认，等待处理。" if action == "确认" else "已取消。")

    def receive_message(self, data):
        message = private_text(data)
        if not message or message[0] != self.approvals.owner_id:
            return
        text = message[1].strip()
        event = data.event.message
        if text == "进度":
            if event.chat_id and event.message_id:
                self.board.request(event.chat_id, event.message_id)
            return
        command = RECIPIENT_COMMAND.fullmatch(text)
        if not command:
            if (
                re.match(r"确认\s+审批[0-9]+(?:\s|$)", text)
                or not event.message_id
                or not event.chat_id
            ):
                return
            references = [ref for ref in (event.parent_id, event.root_id) if ref]
            if references:
                # A delivered message can be quoted before its send call returns.
                with self.delivery_lock:
                    self.dialogs.receive(
                        event.chat_id, event.message_id, text, references
                    )
            else:
                self.dialogs.receive(event.chat_id, event.message_id, text, references)
            return
        number, recipient = command.groups()
        task_id = int(number)
        post_id = (
            self.approvals.awaiting_post_for_task(task_id)
            if task_id <= 2**63 - 1
            else None
        )
        if not post_id:
            print(f"审批{number}不在待确认列表。", flush=True)
            return
        reply = Reply(message[0], self.approvals.inbox_id, post_id, f"确认 {recipient}")
        task = self.approvals.accept_reply(reply, recipient=recipient)
        if task is not None:
            print(f"收到「确认」，审批{task} 将把结论发给指定收件人。", flush=True)

    def deliver(self):
        for task in self.approvals.tasks_in("queued"):
            request = self.approvals.start_approval(task)
            if not request:
                continue
            with self.delivery_lock:
                post_id = self.messages.send("interactive", ticket_card(task, request))
                self.approvals.approval_posted(task, post_id, notice=True)
        for task in self.approvals.tasks_in("ready"):
            delivery = self.approvals.start_result(task)
            if not delivery:
                continue
            _, _, recipient, result, topic = delivery
            with self.delivery_lock:
                target = self.dialogs.reply_target(task)
                if target:
                    # Preserve code and comparisons in text while neutralizing Lark mentions.
                    post_id = self.messages.reply(target, reply_text(result))
                else:
                    header = f"审批{task}"
                    if topic and topic.startswith("ticket:"):
                        header += f" · 工单 #{topic.removeprefix('ticket:')}"
                    result = result.replace("<", "＜").replace(">", "＞")
                    post_id = self.messages.send(
                        "text", {"text": f"{header}\n\n{result}"}, recipient=recipient
                    )
                self.approvals.result_posted(task, post_id)


async def run(pilot, exited):
    async def poll():
        first_success = True
        while True:
            try:
                await asyncio.to_thread(pilot.poll_openproject)
            except BridgeError as error:
                print(
                    f"OpenProject 本轮检查失败，60 秒后再查（{error}）。",
                    file=sys.stderr,
                )
            else:
                if first_success:
                    print("OpenProject 首轮检查已完成。", flush=True)
                    first_success = False
            await asyncio.sleep(60)

    async def execute():
        while True:
            await pilot.execute()
            await asyncio.sleep(0.5)

    async def refresh_board():
        while True:
            await asyncio.to_thread(pilot.board.refresh)
            await asyncio.sleep(REFRESH_SECONDS)

    worker = asyncio.create_task(execute())
    board_task = asyncio.create_task(refresh_board())
    poll_task = asyncio.create_task(poll()) if pilot.op is not None else None
    mm_task = asyncio.create_task(
        watch_mattermost(
            pilot.cursor_path.with_name("mattermost-session.json"), pilot.approvals
        )
    )
    try:
        while not exited.is_set():
            if poll_task is not None and poll_task.done():
                await poll_task
            if worker.done():
                await worker
            if board_task.done():
                await board_task
            if mm_task is not None and mm_task.done():
                error = mm_task.exception()
                detail = f"（{type(error).__name__}）" if error else ""
                print(f"Mattermost 监听已停止{detail}。", file=sys.stderr, flush=True)
                mm_task = None
            await asyncio.to_thread(pilot.deliver)
            await asyncio.sleep(1)
        raise BridgeError("Lark 长连接已停止。")
    finally:
        tasks = [
            task
            for task in (poll_task, worker, mm_task, board_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def import_checkpoint(state_dir):
    # The caller holds the shared run lock. Keep the OP position from the last MM run.
    if (state_dir / "approvals.sqlite3").exists():
        return
    previous = MATTERMOST_STATE / "approvals.sqlite3"
    if previous.exists():
        with closing(
            sqlite3.connect(f"{previous.as_uri()}?mode=ro", uri=True)
        ) as database:
            pending = database.execute(
                "SELECT id FROM task WHERE status NOT IN ('sent', 'cancelled') ORDER BY id"
            ).fetchall()
        if pending:
            labels = "、".join(f"审批{row[0]}" for row in pending)
            raise BridgeError(f"Mattermost 仍有待处理的任务（{labels}）。")
    for filename in ("assigned-tickets.json", "op-cursor.json"):
        original = MATTERMOST_STATE / filename
        target = state_dir / filename
        if original.exists() and not target.exists():
            write_state(target, json.loads(original.read_text()))


def migrate_mattermost(state_dir, owner, inbox):
    # Both services use the same run lock. Publish the database only after the
    # complete snapshot and both OpenProject checkpoints are ready.
    target = state_dir / "approvals.sqlite3"
    if target.exists():
        raise BridgeError("Lark 审批库已存在。")
    previous = MATTERMOST_STATE / target.name
    checkpoints = {
        filename: json.loads((MATTERMOST_STATE / filename).read_text())
        for filename in ("op-cursor.json", "assigned-tickets.json")
    }
    with tempfile.TemporaryDirectory(dir=state_dir) as directory:
        snapshot = Path(directory) / target.name
        with (
            closing(
                sqlite3.connect(f"{previous.as_uri()}?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(snapshot)) as database,
        ):
            source.backup(database)
            with database:
                pending = database.execute(
                    """
                    SELECT id, topic, request, status FROM task
                    WHERE status NOT IN ('sent', 'cancelled') ORDER BY id
                    """
                ).fetchall()
                migrated = []
                for task, topic, payload, status in pending:
                    if status not in {"queued", "awaiting"}:
                        raise BridgeError(f"审批{task} 正处于 {status}，需要先核对。")
                    if topic == "diagnostic":
                        database.execute(
                            "UPDATE task SET status = 'cancelled' WHERE id = ?", (task,)
                        )
                        continue
                    request = json.loads(payload)
                    ticket = re.fullmatch(r"ticket:([1-9][0-9]*)", topic)
                    if (
                        not ticket
                        or request["source"]
                        != f"{OPENPROJECT}/work_packages/{ticket[1]}"
                        or request["channel_id"]
                        or request["root_id"]
                        or request.get("recipient")
                    ):
                        raise BridgeError(
                            f"审批{task} 关联 Mattermost 消息，需要先核对。"
                        )
                    database.execute(
                        "UPDATE task SET status = 'queued', approval_post_id = NULL WHERE id = ?",
                        (task,),
                    )
                    migrated.append((task, ticket[1]))
                if (
                    database.execute(
                        "UPDATE owner SET user_id = ?, inbox_id = ? WHERE singleton = 1",
                        (owner, inbox),
                    ).rowcount
                    != 1
                ):
                    raise BridgeError("无法核对原审批库的绑定信息。")
        for filename, checkpoint in checkpoints.items():
            write_state(state_dir / filename, checkpoint)
        os.chmod(snapshot, 0o600)
        os.replace(snapshot, target)
    return migrated


def configure_parser(parser):
    parser.add_argument(
        "mode", choices=("pair", "check", "migrate", "bind-mm", "pin-progress", "run")
    )


def start(args, *, run_job: RunJob):
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    require_private(STATE_DIR)
    config_path = STATE_DIR / "app.json"
    if args.mode == "bind-mm":
        read_config(config_path)
        with (STATE_DIR / "mattermost-bind.lock").open("a+b") as bind_lock:
            acquire_lock(bind_lock)
            enable_mattermost(STATE_DIR / "mattermost-session.json")
        return
    if args.mode == "pin-progress":
        read_config(config_path)
        (STATE_DIR / PIN_MARKER).touch(mode=0o600, exist_ok=True)
        print("已启用 Lark 看板置顶，后台服务将在下次启动时发布。")
        return
    with (STATE_DIR / "lock").open("a+b") as lark_lock:
        acquire_lock(lark_lock)
        if args.mode == "pair":
            pair(config_path)
            return
        config = read_config(config_path)
        messages = LarkMessages(config)
        if args.mode == "check":
            check(config, messages)
            return
        with (MATTERMOST_STATE / "lock").open("a+b") as run_lock:
            try:
                acquire_lock(run_lock)
            except BlockingIOError:
                raise BridgeError(
                    "Mattermost 审批服务仍在运行，请先停掉它再切换。"
                ) from None
            op = None
            if (
                args.mode == "migrate"
                or OPENPROJECT
                or OPENPROJECT_USER_ID is not None
                or SETTINGS.openproject_api_key
            ):
                op = openproject_client()
                if op.request("/api/v3/users/me")["id"] != OPENPROJECT_USER_ID:
                    raise BridgeError("OpenProject 登录身份与配置的用户不符。")
            inbox = f"{INBOX}:{config['app_id']}"
            if args.mode == "migrate":
                for task, ticket in migrate_mattermost(
                    STATE_DIR, messages.owner, inbox
                ):
                    print(
                        f"已迁移审批{task}（工单 #{ticket}），等待 Lark 发卡。",
                        flush=True,
                    )
                return
            import_checkpoint(STATE_DIR)
            approvals = Approvals(
                STATE_DIR / "approvals.sqlite3", messages.owner, inbox
            )
            for status in IN_FLIGHT:
                if approvals.tasks_in(status):
                    raise BridgeError(f"有中断的 {status} 任务，请先核对状态。")
            pilot = LarkChannel(
                op,
                messages,
                approvals,
                STATE_DIR / "op-cursor.json",
                run_job=run_job,
                pin_progress=(STATE_DIR / PIN_MARKER).is_file(),
            )
            handler = (
                dispatcher()
                .register_p2_card_action_trigger(pilot.receive)
                .register_p2_im_message_receive_v1(pilot.receive_message)
                .build()
            )
            with (STATE_DIR / "worker.lock").open("a+b") as running:
                acquire_lock(running, blocking=True)
                with long_connection(config, handler) as exited:
                    print(
                        "Lark 对话服务已启动。"
                        + ("OpenProject 每 60 秒检查一次。" if op is not None else ""),
                        flush=True,
                    )
                    asyncio.run(run(pilot, exited))
