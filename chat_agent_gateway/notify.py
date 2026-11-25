#!/usr/bin/env python3
"""Forward the operator's OpenProject notifications to a Mattermost bot DM."""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import SETTINGS
from .credentials import read_secret
from .local import acquire_lock, require_private
from .paths import STATE_DIR

OPENPROJECT = SETTINGS.openproject_origin
MATTERMOST = SETTINGS.mattermost_origin
OPENPROJECT_USER_ID = SETTINGS.openproject_user_id
RECIPIENT_EMAIL = SETTINGS.mattermost_email
KEYCHAIN_SERVICE = "chat-agent-gateway-mattermost-notifier"
DEFAULT_STATE = STATE_DIR / "mattermost-notifier"
REASONS = {
    "assigned": "分配给你的工单",
    "mentioned": "有人 @你",
    "responsible": "你负责的工单",
    "watched": "你关注的工单",
}
WORK_PACKAGE_PATH = re.compile(r"/api/v3/work_packages/([1-9][0-9]*)")


class BridgeError(Exception):
    pass


class ApiError(BridgeError):
    def __init__(self, origin, status):
        self.origin = origin
        self.status = status
        super().__init__(f"{origin}: HTTP {status}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Api:
    def __init__(self, origin, authorization):
        self.origin = origin
        self.authorization = authorization
        self.opener = build_opener(NoRedirect)

    def request(self, path, body=None):
        payload, _ = self.request_with_headers(path, body)
        return payload

    def request_with_headers(self, path, body=None, *, extra_headers=None):
        relative = urlsplit(path)
        if relative.scheme or relative.netloc or not relative.path.startswith("/api/"):
            raise BridgeError("API returned an unexpected link")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.authorization:
            headers["Authorization"] = self.authorization
        if extra_headers:
            headers.update(extra_headers)
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.origin + path, headers=headers, data=data)
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response), response.headers
        except HTTPError as error:
            raise ApiError(self.origin, error.code) from None
        except (OSError, HTTPException, ValueError) as error:
            raise BridgeError(
                f"{self.origin}: connection or response error ({type(error).__name__})"
            ) from None


def openproject_client():
    key = SETTINGS.openproject_api_key
    if (
        not OPENPROJECT
        or OPENPROJECT_USER_ID is None
        or not key
        or key == "replace-with-api-key"
    ):
        raise BridgeError("请在本机配置 OpenProject origin、user_id 和 api_key。")
    token = base64.b64encode(f"apikey:{key}".encode()).decode()
    return Api(OPENPROJECT, f"Basic {token}")


def mattermost_client(service=KEYCHAIN_SERVICE):
    if not MATTERMOST or not RECIPIENT_EMAIL:
        raise BridgeError("请在本机配置 Mattermost origin 和 email。")
    try:
        token = read_secret(service, RECIPIENT_EMAIL if os.name == "nt" else None)
    except OSError as error:
        raise BridgeError(str(error)) from error
    if not token:
        raise BridgeError("Mattermost 会话凭据为空。")
    return Api(MATTERMOST, f"Bearer {token}")


def notification_path(page_size, reasons=REASONS):
    query = urlencode(
        {
            "pageSize": page_size,
            "sortBy": json.dumps([["id", "desc"]]),
            "filters": json.dumps(
                [{"reason": {"operator": "=", "values": list(reasons)}}]
            ),
        }
    )
    return "/api/v3/notifications?" + query


def newest_notification(api, reasons=REASONS):
    page = api.request(notification_path(1, reasons))
    items = page["_embedded"]["elements"]
    return (items[0]["id"] if items else 0), page["total"]


def notifications_after(api, cursor, reasons=REASONS):
    path = notification_path(100, reasons)
    pending = []
    last_id = None
    total = None
    visited = set()
    while path:
        if path in visited or urlsplit(path).path != "/api/v3/notifications":
            raise BridgeError("OpenProject returned an invalid notification page")
        visited.add(path)
        page = api.request(path)
        if total is None:
            total = page["total"]
        elif total != page["total"]:
            raise BridgeError("OpenProject notifications changed during pagination")
        for item in page["_embedded"]["elements"]:
            current_id = item["id"]
            if last_id is not None and current_id >= last_id:
                raise BridgeError(
                    "OpenProject notification order changed during pagination"
                )
            last_id = current_id
            if current_id <= cursor:
                path = None
                break
            pending.append(item)
        else:
            path = page["_links"].get("nextByOffset", {}).get("href")
            if not path and len(pending) != total:
                raise BridgeError("OpenProject notification pages ended early")

    expected_head = pending[0]["id"] if pending else last_id or 0
    if newest_notification(api, reasons) != (expected_head, total):
        raise BridgeError("OpenProject notifications changed during polling")
    return reversed(pending)


def message_for(item):
    resource = item["_links"]["resource"]
    match = WORK_PACKAGE_PATH.fullmatch(resource.get("href", ""))
    if not match or item["reason"] not in REASONS:
        raise BridgeError(f"Unsupported OpenProject notification {item['id']}")
    work_package_id = match[1]
    title = " ".join((resource.get("title") or "").split())
    escaped_title = re.sub(r"([\\`*_{}\[\]()#+!<>@|~])", r"\\\1", title)
    return (
        f"**{REASONS[item['reason']]}**\n"
        f"[#{work_package_id}]({OPENPROJECT}/work_packages/{work_package_id}) "
        f"{escaped_title}"
    ).rstrip()


def write_state(path, state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def poll(openproject, mattermost, state_path, bot_id, recipient_id):
    identity = [bot_id, recipient_id]
    if not state_path.exists():
        latest, _ = newest_notification(openproject)
        write_state(
            state_path, {"identity": identity, "cursor": latest, "pending": None}
        )
        print(f"Notification baseline saved at {latest}", flush=True)
        return

    state = json.loads(state_path.read_text())
    if state["identity"] != identity:
        raise BridgeError("The saved cursor is bound to a different Mattermost DM")
    if state["pending"] is not None:
        raise BridgeError(
            f"Delivery of notification {state['pending']} needs reconciliation"
        )

    items = list(notifications_after(openproject, state["cursor"]))
    if not items:
        return
    channel = mattermost.request("/api/v4/channels/direct", identity)
    if channel.get("type") != "D" or not channel.get("id"):
        raise BridgeError("Mattermost did not return a direct message channel")
    if set(channel.get("name", "").split("__")) != set(identity):
        raise BridgeError("Mattermost returned a DM with unexpected participants")

    for item in items:
        notification_id = item["id"]
        message = message_for(item)
        state["pending"] = notification_id
        write_state(state_path, state)
        post = mattermost.request(
            "/api/v4/posts",
            {
                "channel_id": channel["id"],
                "message": message,
                "props": {"openproject_notification_id": notification_id},
            },
        )
        if not post.get("id") or post.get("channel_id") != channel["id"]:
            raise BridgeError(
                f"Unexpected Mattermost response for notification {notification_id}"
            )
        state.update(cursor=notification_id, pending=None)
        write_state(state_path, state)
        print(f"Forwarded notification {notification_id}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "check", "once", "run"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()

    openproject = openproject_client()
    if openproject.request("/api/v3/users/me")["id"] != OPENPROJECT_USER_ID:
        raise BridgeError("OpenProject 登录身份与配置的用户不符。")
    if args.mode == "preview":
        latest, total = newest_notification(openproject)
        print(
            f"OpenProject user {OPENPROJECT_USER_ID}: {total} matching notifications, newest {latest}"
        )
        return

    mattermost = mattermost_client()
    bot = mattermost.request("/api/v4/users/me")
    if bot.get("is_bot") is not True:
        raise BridgeError("The Mattermost token must belong to a bot account")
    recipient = mattermost.request(
        "/api/v4/users/email/" + quote(RECIPIENT_EMAIL, safe="")
    )
    if (
        recipient.get("email", "").casefold() != RECIPIENT_EMAIL
        or bot["id"] == recipient["id"]
    ):
        raise BridgeError("Mattermost recipient could not be verified by email")
    print(
        f"Verified Mattermost bot @{bot['username']} and recipient @{recipient['username']}",
        flush=True,
    )
    if args.mode == "check":
        return

    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private(args.state_dir)
    with (args.state_dir / "lock").open("a+b") as lock:
        try:
            acquire_lock(lock)
        except BlockingIOError:
            raise BridgeError(
                "Another notification process is already running"
            ) from None
        while True:
            poll(
                openproject,
                mattermost,
                args.state_dir / "state.json",
                bot["id"],
                recipient["id"],
            )
            if args.mode == "once":
                return
            time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BridgeError as error:
        sys.exit(f"Notification bridge stopped: {error}")
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        sys.exit(f"Notification bridge stopped: {type(error).__name__}")
