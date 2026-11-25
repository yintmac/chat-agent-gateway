"""Run the local, approval-gated OpenProject / Mattermost pilot."""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import warnings
from contextlib import asynccontextmanager, contextmanager
from email.utils import parsedate_to_datetime
from getpass import GetPassWarning, getpass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlsplit

from ..approval import IN_FLIGHT, Approvals, Reply, Request
from ..local import acquire_lock, require_private
from ..notify import (
    MATTERMOST,
    OPENPROJECT,
    OPENPROJECT_USER_ID,
    RECIPIENT_EMAIL,
    Api,
    ApiError,
    BridgeError,
    newest_notification,
    openproject_client,
)
from ..openproject_watch import assigned_open_tickets, ticket_summary
from ..paths import PROJECT_DIR, STATE_DIR
from ..workspaces import (
    BRANCHES,
    available_repositories,
    mentioned_repository,
    workspace_for,
)
from ..workflow import REASONS, ApprovalWorkflow, RunJob

ENTRY_MODULE = "chat_agent_gateway"
MATTERMOST_ID = re.compile(r"^[a-z0-9]{26}$")
USERNAME = re.compile(r"^@[a-z0-9._-]{1,64}$")
SELF_CHAT_COMMAND = re.compile(r"(确认|取消)\s+审批([1-9][0-9]{0,18})((?:\s+\S+){0,2})")
OP_HOST = urlsplit(OPENPROJECT).netloc
OP_LINK = rf"(?<![\w.-]){re.escape(OP_HOST)}/work_packages/|" if OP_HOST else ""
TICKET_REF = re.compile(rf"(?:{OP_LINK}(?<!\w)#)([1-9][0-9]*)")


def mattermost_expiry(headers, token):
    for header in headers.get_all("Set-Cookie", []):
        try:
            cookie = SimpleCookie(header).get("MMAUTHTOKEN")
            if cookie and cookie.value == token and cookie["expires"]:
                return parsedate_to_datetime(cookie["expires"]).astimezone()
        except (CookieError, ValueError):
            continue
    return None


def sign_in_mattermost():
    if not MATTERMOST or not RECIPIENT_EMAIL:
        raise BridgeError("请在本机配置 Mattermost origin 和 email。")
    if not sys.stdin.isatty():
        raise BridgeError("请在终端运行，并在本机输入 Mattermost 密码和验证码。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass(f"Mattermost 密码（{RECIPIENT_EMAIL}）: ")
            code = getpass("Mattermost 双重验证的 6 位验证码: ").strip()
    except GetPassWarning:
        raise BridgeError("终端无法隐藏输入，登录已停止。") from None
    if not password or not re.fullmatch(r"[0-9]{6}", code):
        raise BridgeError("请输入密码和 6 位验证码。")
    try:
        _, headers = Api(MATTERMOST, None).request_with_headers(
            "/api/v4/users/login",
            {"login_id": RECIPIENT_EMAIL, "password": password, "token": code},
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
        )
    except ApiError as error:
        if error.status in (400, 401, 403):
            raise BridgeError(
                "Mattermost 登录失败，请核对密码和当前验证码后重新运行。"
            ) from None
        raise

    token = headers.get("Token", "")
    if not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
        raise BridgeError("Mattermost 登录响应里没有有效的会话凭据。")
    return Api(MATTERMOST, f"Bearer {token}"), mattermost_expiry(headers, token)


def mattermost_identity(mm):
    person = mm.request("/api/v4/users/me")
    if person.get("is_bot") or person.get("email", "").casefold() != RECIPIENT_EMAIL:
        raise BridgeError("Mattermost 登录身份与审批账号不符。")
    mm_id(person.get("id"))
    return person


@contextmanager
def mattermost_session(authorization=None):
    if not MATTERMOST or not RECIPIENT_EMAIL:
        raise BridgeError("请在本机配置 Mattermost origin 和 email。")
    if authorization is None:
        mm, expires = sign_in_mattermost()
    else:
        mm, expires = Api(MATTERMOST, authorization), None
    try:
        try:
            person = mattermost_identity(mm)
        except ApiError as error:
            if authorization is not None and error.status == 401:
                raise BridgeError("Mattermost 会话已失效，请重新运行并登录。") from None
            raise
        name = person["username"]
        deadline = (
            expires.strftime("%Y-%m-%d %H:%M:%S %Z") if expires else "服务器未提供"
        )
        if authorization is None:
            print(
                f"Mattermost 已登录为 @{name}。本次登录返回的到期时间 {deadline}。",
                flush=True,
            )
        else:
            print(f"已用当前 Mattermost 会话重新连接 @{name}。", flush=True)
        yield mm, person
    finally:
        try:
            mm.request("/api/v4/users/logout", {})
        except ApiError as error:
            if error.status != 401:
                print(
                    f"Mattermost 会话注销失败，HTTP {error.status}。", file=sys.stderr
                )
        except BridgeError:
            print(
                "Mattermost 会话注销失败，请在安全设置里核对活动会话。", file=sys.stderr
            )


class ReloadRequested(Exception):
    pass


def code_revision():
    files = []
    package = Path(__file__).resolve().parents[1]
    for path in sorted(package.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        files.append(
            (path.relative_to(package).as_posix(), info.st_mtime_ns, info.st_size)
        )
    return tuple(files)


def clean_python_args():
    # Each check and reload reads fresh source, even for same-second, same-size edits.
    cache = Path(tempfile.gettempdir()) / f"pilot-reload-{secrets.token_hex(8)}"
    return [sys.executable, "-B", "-X", f"pycache_prefix={cache}"]


def reload_check():
    try:
        result = subprocess.run(
            [
                *clean_python_args(),
                "-c",
                f"import {ENTRY_MODULE}.__main__\nimport {ENTRY_MODULE}.channels.mattermost\nimport websockets.asyncio.client",
            ],
            cwd=PROJECT_DIR,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def read_reload_session(fd):
    with os.fdopen(fd, "r", encoding="utf-8") as reader:
        payload = json.load(reader)
    if not isinstance(payload, dict):
        raise BridgeError("自动重载的会话数据无效。")
    authorization = payload.get("authorization")
    lock_fd = payload.get("lock_fd")
    if (
        not isinstance(lock_fd, int)
        or lock_fd < 3
        or not isinstance(authorization, str)
        or not re.fullmatch(r"Bearer [A-Za-z0-9._~-]+", authorization)
    ):
        raise BridgeError("自动重载的会话数据无效。")
    return authorization, lock_fd


def restart_with_session(mm, lock, state_dir, *, backend):
    # Carry the active session only through an anonymous pipe and retain the lock.
    read_fd, write_fd = os.pipe()
    lock_fd = lock.fileno()
    previous = os.get_inheritable(lock_fd)
    try:
        with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
            json.dump({"authorization": mm.authorization, "lock_fd": lock_fd}, writer)
        os.set_inheritable(read_fd, True)
        os.set_inheritable(lock_fd, True)
        argv = [
            *clean_python_args(),
            "-m",
            ENTRY_MODULE,
            "--backend",
            backend,
            "mattermost",
            "run",
            "--state-dir",
            str(state_dir),
            "--resume-fd",
            str(read_fd),
        ]
        os.execv(sys.executable, argv)
    finally:
        os.set_inheritable(lock_fd, previous)
        os.close(read_fd)


def mm_id(value):
    if not isinstance(value, str) or not MATTERMOST_ID.fullmatch(value):
        raise BridgeError("Mattermost 返回了无法核对的 ID。")
    return value


def post(api, channel_id, message, root_id=None):
    payload = {"channel_id": mm_id(channel_id), "message": message}
    if root_id:
        payload["root_id"] = mm_id(root_id)
    result = api.request("/api/v4/posts", payload)
    if result.get("channel_id") != channel_id or result.get("root_id", "") != (
        root_id or ""
    ):
        raise BridgeError("Mattermost 回执与预期线程不符。")
    return mm_id(result.get("id"))


def direct_channel(api, own_id, other_id):
    pair = [mm_id(own_id), mm_id(other_id)]
    channel = api.request("/api/v4/channels/direct", pair)
    if channel.get("type") != "D" or channel.get("name") != "__".join(sorted(pair)):
        raise BridgeError("Mattermost 回执中的私聊对方不符。")
    return mm_id(channel.get("id"))


def plain_message(value, limit):
    # A forwarded quote must not render actionable links or mention other users.
    text = value.replace("@", "＠").replace("<", "＜").replace(">", "＞")
    text = re.sub(r"(https?):(?=//)", r"\1：", text, flags=re.IGNORECASE)
    text = re.sub(r"([\\`*_{}\[\]()#+!|~])", r"\\\1", text)
    if len(text) > limit:
        text = text[:limit] + "…（原文见链接）"
    return text


def mattermost_request(record, mentions, owner, username, op):
    if record["user_id"] == owner or owner not in mentions or record.get("type"):
        return None
    post_id = mm_id(record["id"])
    message = record["message"]
    if not re.search(
        rf"(?<![\w@])@{re.escape(username)}(?![\w.-])", message, re.IGNORECASE
    ):
        return None
    tickets = sorted(set(TICKET_REF.findall(message)))
    if len(tickets) == 1 and op is not None:
        ticket = op.request(f"/api/v3/work_packages/{tickets[0]}")
        message += "\n\n" + ticket_summary(ticket)
    return Request(
        event_id=f"mm:{post_id}",
        topic=f"ticket:{tickets[0]}" if len(tickets) == 1 else f"mm:{post_id}",
        source=f"{MATTERMOST}/_redirect/pl/{post_id}",
        summary=message,
        channel_id=mm_id(record["channel_id"]),
        root_id=mm_id(record.get("root_id") or post_id),
    )


def approval_text(request, task_id):
    destination = "原 Mattermost 线程" if request["channel_id"] else "你的自聊"
    command = f"确认 审批{task_id}"
    workspace = request.get("suggested_workspace")
    if workspace:
        branch = BRANCHES.get(workspace)
        target = f"`{workspace}`" + (f" 的 `{branch}`" if branch else "")
        instructions = f"发送 `{command}` 就在 {target} 处理，结果发到{destination}。\n"
    elif request.get("auto_route"):
        instructions = f"发送 `{command}` 后，会先在配置的工作区查仓库。目标明确就处理，结果发到{destination}。拿不准会来问你。\n"
    else:
        instructions = f"发送 `{command}` 就开始分析，结果发到{destination}。\n"
    return (
        f"**审批{task_id}** 等你确认。[看原文]({request['source']})\n\n"
        f"{plain_message(request['summary'], 2500)}\n\n"
        "在自聊底部直接发指令。\n"
        f"{instructions}"
        "要指定其他仓库或私信收件人，可在确认后加仓库名或 `@用户名`。"
        f"发送 `取消 审批{task_id}` 跳过。"
    )


def parse_reply(reply, api):
    parts = reply.message.split()
    if not parts or parts[0] not in {"确认", "取消"}:
        return None
    if parts[0] == "取消" and len(parts) == 1:
        return None, None
    if parts[0] != "确认" or len(parts) > 3:
        raise BridgeError("请回「确认 [仓库目录名] [@用户名]」或「取消」。")
    recipient = None
    if len(parts) > 1 and USERNAME.fullmatch(parts[-1]):
        username = parts.pop()[1:]
        user = api.request("/api/v4/users/username/" + quote(username, safe=""))
        if user.get("username") != username or user.get("is_bot"):
            raise BridgeError("找不到该收件人。")
        recipient = mm_id(user.get("id"))
    if len(parts) > 2:
        raise BridgeError("请每次选一个仓库和一个收件人。")
    workspace = parts[1] if len(parts) == 2 else None
    if workspace:
        workspace_for(workspace)
    return workspace, recipient


class MattermostChannel(ApprovalWorkflow):
    def __init__(self, op, mm, approvals, cursor_path, username, *, run_job: RunJob):
        super().__init__(op, approvals, cursor_path, run_job)
        self.mm = mm
        self.username = username
        self.approval_lock = Lock()

    def recover_cancellations(self):
        owner = self.approvals.owner_id
        inbox = self.approvals.inbox_id
        count = 0
        for root_id in self.approvals.awaiting_post_ids():
            try:
                thread = self.mm.request(
                    f"/api/v4/posts/{mm_id(root_id)}/thread?perPage=0"
                )
            except ApiError as error:
                if error.status not in (403, 404):
                    raise
                print(
                    f"暂时无法核对审批线程 {root_id}，HTTP {error.status}。",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            records = thread.get("posts") or {}
            root = records.get(root_id, {})
            if root.get("channel_id") != inbox or root.get("user_id") != owner:
                raise BridgeError("Mattermost 返回的审批原帖与自聊不符。")
            for record in records.values():
                if (
                    record.get("user_id") == owner
                    and record.get("channel_id") == inbox
                    and record.get("root_id") == root_id
                    and not record.get("delete_at")
                    and record.get("message", "").strip() == "取消"
                ):
                    if (
                        self.approvals.accept_reply(
                            Reply(owner, inbox, root_id, "取消")
                        )
                        is not None
                    ):
                        count += 1
                    break
        if count:
            print(f"已从 Mattermost 原线程恢复 {count} 条取消。", flush=True)

    def receive(self, event):
        if event.get("event") != "posted":
            return
        data = event["data"]
        record = json.loads(data["post"])
        owner = self.approvals.owner_id
        if record["user_id"] == owner:
            if record["channel_id"] != self.approvals.inbox_id or record.get("type"):
                return
            message = record["message"].strip()
            root = record.get("root_id")
            from_thread = bool(root)
            with self.approval_lock:
                if root:
                    if not self.approvals.is_awaiting(root):
                        return
                else:
                    command = SELF_CHAT_COMMAND.fullmatch(message)
                    if not command:
                        if message.split()[:1] in (["确认"], ["取消"]):
                            print(
                                "请带上审批号，例如「确认 审批2」或「取消 审批2」。",
                                flush=True,
                            )
                        return
                    action, number, options = command.groups()
                    approval_id = int(number)
                    root = (
                        self.approvals.awaiting_post_for_task(approval_id)
                        if approval_id <= 2**63 - 1
                        else None
                    )
                    if not root:
                        print(f"审批{number}不在待确认列表。", flush=True)
                        return
                    message = action + options
            reply = Reply(owner, record["channel_id"], root, message)
            try:
                inputs = parse_reply(reply, self.mm)
            except (BridgeError, subprocess.SubprocessError):
                post(
                    self.mm,
                    reply.channel_id,
                    "没识别到可用的仓库或收件人。请检查后再回。",
                    reply.root_id if from_thread else None,
                )
                return
            if inputs is not None:
                task_id = self.approvals.accept_reply(
                    reply, workspace=inputs[0], recipient=inputs[1]
                )
                if task_id is not None:
                    print(
                        f"收到「{reply.message.split()[0]}」，审批任务 {task_id}。",
                        flush=True,
                    )
            return

        mentions = json.loads(data.get("mentions") or "[]")
        request = mattermost_request(record, mentions, owner, self.username, self.op)
        if request:
            self.approvals.enqueue(request)

    def deliver(self):
        for task_id in self.approvals.tasks_in("queued"):
            request = self.approvals.start_approval(task_id)
            if not request:
                continue
            workspace = mentioned_repository(
                request["summary"], available_repositories()
            )
            auto_route = not workspace and request["topic"] != "diagnostic"
            self.approvals.record_approval_scope(
                task_id, workspace, auto_route=auto_route
            )
            request.update(suggested_workspace=workspace, auto_route=auto_route)
            with self.approval_lock:
                root = post(
                    self.mm, self.approvals.inbox_id, approval_text(request, task_id)
                )
                self.approvals.approval_posted(task_id, root)
        for task_id in self.approvals.tasks_in("ready"):
            delivery = self.approvals.start_result(task_id)
            if not delivery:
                continue
            channel, root, recipient, message, topic = delivery
            if recipient:
                channel = direct_channel(self.mm, self.approvals.owner_id, recipient)
                root = None
            message = plain_message(message, 3500)
            if channel == self.approvals.inbox_id:
                root = None
                message = f"**审批{task_id}**\n\n{message}"
            ticket = topic.removeprefix("ticket:") if topic else ""
            if ticket.isdecimal():
                message += f"\n\n[工单 #{ticket}]({OPENPROJECT}/work_packages/{ticket})"
            result = post(self.mm, channel, message, root)
            self.approvals.result_posted(task_id, result)


@asynccontextmanager
async def mattermost_socket(mm):
    from websockets.asyncio.client import connect

    url = MATTERMOST.replace("https://", "wss://", 1) + "/api/v4/websocket"
    async with connect(url, proxy=None) as socket:
        if (
            socket.request.headers["Host"] != urlsplit(MATTERMOST).netloc
            or socket.transport.get_extra_info("ssl_object") is None
        ):
            raise BridgeError("Mattermost WebSocket 地址与预期不符。")
        token = mm.authorization.removeprefix("Bearer ")
        await socket.send(
            json.dumps(
                {
                    "seq": 1,
                    "action": "authentication_challenge",
                    "data": {"token": token},
                }
            )
        )
        acknowledged = False
        identified = False
        async with asyncio.timeout(15):
            while not (acknowledged and identified):
                response = json.loads(await socket.recv())
                if response.get("seq_reply") == 1:
                    if response.get("status") != "OK":
                        raise BridgeError("Mattermost WebSocket 登录失败。")
                    acknowledged = True
                if response.get("event") == "hello":
                    identified = True
        yield socket


async def listen(pilot, connected):
    async with mattermost_socket(pilot.mm) as socket:
        await asyncio.to_thread(pilot.recover_cancellations)
        connected.set()
        print("审批服务已连接。OpenProject 每分钟检查一次。", flush=True)
        async for message in socket:
            await asyncio.to_thread(pilot.receive, json.loads(message))
    raise BridgeError("Mattermost 已断开，离线期间的消息需要核对后再恢复。")


async def poll(pilot):
    while True:
        await asyncio.to_thread(pilot.mm.request, "/api/v4/users/me")
        try:
            await asyncio.to_thread(pilot.poll_openproject)
        except BridgeError as error:
            print(
                f"OpenProject 本轮检查失败（{error}）。Mattermost 继续监听，60 秒后再检查 OpenProject。",
                file=sys.stderr,
                flush=True,
            )
        await asyncio.sleep(60)


async def watch_code(revision, work_gate, delivery_gate, stopping):
    attempted = revision
    while True:
        await asyncio.sleep(1)
        candidate = code_revision()
        if candidate == revision or candidate == attempted:
            continue
        # Wait for saves across the files to settle before validating the new code.
        await asyncio.sleep(1)
        if code_revision() != candidate:
            continue
        attempted = candidate
        if not await asyncio.to_thread(reload_check):
            print(
                "新版导入检查没通过，审批服务继续运行，等文件改好再加载。",
                file=sys.stderr,
                flush=True,
            )
            continue
        async with work_gate, delivery_gate:
            if code_revision() != candidate:
                continue
            stopping.set()
            raise ReloadRequested


async def run_worker(operation, gate, stopping):
    while not stopping.is_set():
        async with gate:
            if stopping.is_set():
                return
            await operation()
        await asyncio.sleep(1)


async def run(pilot, revision=None):
    connected = asyncio.Event()
    stopping = asyncio.Event()
    work_gate = asyncio.Lock()
    delivery_gate = asyncio.Lock()
    reloading = False

    async def after_connect(action, *args):
        await connected.wait()
        await action(*args)

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(listen(pilot, connected))
            group.create_task(after_connect(poll, pilot))
            group.create_task(
                after_connect(
                    run_worker,
                    lambda: asyncio.to_thread(pilot.deliver),
                    delivery_gate,
                    stopping,
                )
            )
            group.create_task(
                after_connect(run_worker, pilot.execute, work_gate, stopping)
            )
            if revision is not None:
                group.create_task(
                    after_connect(
                        watch_code, revision, work_gate, delivery_gate, stopping
                    )
                )
    except* ReloadRequested:
        reloading = True
    except* BridgeError as errors:
        messages = []
        for error in errors.exceptions:
            if (
                isinstance(error, ApiError)
                and error.origin == MATTERMOST
                and error.status == 401
            ):
                messages.append("Mattermost 会话已失效，请重新运行并登录。")
            else:
                messages.append(str(error))
        raise BridgeError("\n".join(messages)) from None
    return reloading


def configure_parser(parser):
    import argparse

    parser.add_argument("mode", choices=("preview", "check", "run"))
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--resume-fd", type=int, help=argparse.SUPPRESS)


def start(args, *, run_job: RunJob):
    if args.resume_fd is not None and (args.mode != "run" or os.name != "posix"):
        raise BridgeError("--resume-fd only applies to POSIX run.")
    authorization, lock_fd = (
        read_reload_session(args.resume_fd)
        if args.resume_fd is not None
        else (None, None)
    )
    revision = code_revision() if args.mode == "run" and os.name == "posix" else None
    op = openproject_client()
    if op.request("/api/v3/users/me")["id"] != OPENPROJECT_USER_ID:
        raise BridgeError("OpenProject 登录身份与配置的用户不符。")
    if args.mode == "preview":
        _, total = newest_notification(op, REASONS)
        print(f"OpenProject user {OPENPROJECT_USER_ID}: {total} matching notifications")
        print(f"Open tickets assigned to this user: {len(assigned_open_tickets(op))}")
        return
    if args.mode == "check":
        with mattermost_session():
            print("OpenProject 和 Mattermost 登录验证通过。", flush=True)
        return
    args.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    require_private(args.state_dir)
    lock_path = args.state_dir / "lock"
    with (
        os.fdopen(lock_fd, "a+b") if lock_fd is not None else lock_path.open("a+b")
    ) as lock:
        if not os.path.samestat(os.fstat(lock.fileno()), lock_path.stat()):
            raise BridgeError("自动重载的运行锁与当前审批目录不符。")
        os.set_inheritable(lock.fileno(), False)
        acquire_lock(lock)
        if (args.state_dir / "lark" / "approvals.sqlite3").exists():
            raise BridgeError("审批服务已切换到 Lark。")
        with mattermost_session(authorization) as (mm, person):
            owner = person["id"]
            inbox = direct_channel(mm, owner, owner)
            approvals = Approvals(args.state_dir / "approvals.sqlite3", owner, inbox)
            for state in IN_FLIGHT:
                if approvals.tasks_in(state):
                    raise BridgeError(f"有中断的 {state} 任务，请先核对状态。")
            reloading = asyncio.run(
                run(
                    MattermostChannel(
                        op,
                        mm,
                        approvals,
                        args.state_dir / "op-cursor.json",
                        person["username"],
                        run_job=run_job,
                    ),
                    revision,
                )
            )
            if reloading:
                print("正在加载新版，继续使用当前登录会话。", flush=True)
                restart_with_session(mm, lock, args.state_dir, backend=args.backend)
