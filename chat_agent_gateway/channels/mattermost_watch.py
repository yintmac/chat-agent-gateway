"""Read personal Mattermost mentions for the Lark approval queue."""

import asyncio
import json
import re
import subprocess
import sys
from time import time

from websockets.exceptions import WebSocketException

from ..credentials import read_secret, write_secret
from ..notify import (
    MATTERMOST,
    RECIPIENT_EMAIL,
    Api,
    ApiError,
    BridgeError,
    write_state,
)
from .mattermost import (
    mattermost_identity,
    mattermost_request,
    mattermost_socket,
    sign_in_mattermost,
)

KEYCHAIN_SERVICE = "chat-agent-gateway-lark-mattermost-session"
TOKEN = re.compile(r"[A-Za-z0-9._~-]+")


def stored_token():
    token = read_secret(KEYCHAIN_SERVICE, RECIPIENT_EMAIL)
    if not TOKEN.fullmatch(token):
        raise BridgeError("Mattermost 会话凭据格式有误。")
    return token


def enable_mattermost(path):
    mm, expires = sign_in_mattermost()
    person = mattermost_identity(mm)

    async def check_connection():
        async with mattermost_socket(mm):
            pass

    asyncio.run(check_connection())
    token = mm.authorization.removeprefix("Bearer ")
    write_secret(KEYCHAIN_SERVICE, RECIPIENT_EMAIL, token)
    if stored_token() != token:
        raise BridgeError("Mattermost 会话凭据写入后无法核验。")
    write_state(path, {"expires": expires.timestamp() if expires else None})
    deadline = expires.strftime("%Y-%m-%d %H:%M:%S %Z") if expires else "服务器未提供"
    print(
        f"Mattermost 已绑定为 @{person['username']}。会话到期时间 {deadline}。",
        flush=True,
    )


def generation(path):
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def enqueue_mention(event, person, approvals):
    if event.get("event") != "posted":
        return
    data = event["data"]
    record = json.loads(data["post"])
    mentions = json.loads(data.get("mentions") or "[]")
    request = mattermost_request(
        record, mentions, person["id"], person["username"], None
    )
    if request:
        approvals.enqueue(request)


async def watch_mattermost(path, approvals):
    if not MATTERMOST or not RECIPIENT_EMAIL:
        return
    paused = None
    had_connection = False
    while True:
        stamp = generation(path)
        if stamp is None or stamp == paused:
            await asyncio.sleep(5)
            continue
        try:
            expires = json.loads(path.read_text())["expires"]
            if expires is not None and time() >= expires:
                raise BridgeError("Mattermost 会话已到期，请重新绑定。")
            token = await asyncio.to_thread(stored_token)
        except (
            BridgeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            subprocess.SubprocessError,
        ) as error:
            print(f"Mattermost 监听暂停（{error}）。", file=sys.stderr, flush=True)
            paused = stamp
            continue

        mm = Api(MATTERMOST, f"Bearer {token}")
        try:
            person = await asyncio.to_thread(mattermost_identity, mm)
            async with mattermost_socket(mm) as socket:
                had_connection = True
                print("Mattermost @ 提醒监听已连接。", flush=True)
                while generation(path) == stamp:
                    if expires is not None and time() >= expires:
                        raise ApiError(MATTERMOST, 401)
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=10)
                    except TimeoutError:
                        continue
                    await asyncio.to_thread(
                        enqueue_mention, json.loads(raw), person, approvals
                    )
        except ApiError as error:
            if error.status in (401, 403):
                print(
                    "Mattermost 会话已失效，请重新绑定。", file=sys.stderr, flush=True
                )
                paused = stamp
                continue
            print(
                f"Mattermost 访问失败（HTTP {error.status}），15 秒后重新连接。",
                file=sys.stderr,
                flush=True,
            )
        except (
            BridgeError,
            OSError,
            TimeoutError,
            ValueError,
            KeyError,
            WebSocketException,
        ) as error:
            if had_connection:
                message = "Mattermost 已断开，离线期间的 @ 消息需在 Mattermost 核对"
                had_connection = False
            else:
                message = "Mattermost 连接失败"
            print(
                f"{message}（{type(error).__name__}），15 秒后重新连接。",
                file=sys.stderr,
                flush=True,
            )
        else:
            continue
        await asyncio.sleep(15)
