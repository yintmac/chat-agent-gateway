"""Read installation settings from a private file."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .local import require_private
from .paths import PROJECT_DIR

CONFIG_PATH = (
    Path(
        os.environ.get(
            "CHAT_AGENT_GATEWAY_CONFIG", str(PROJECT_DIR / "config.local.json")
        )
    )
    .expanduser()
    .absolute()
)


@dataclass(frozen=True)
class Settings:
    openproject_origin: str = ""
    openproject_user_id: int | None = None
    openproject_api_key: str = field(default="", repr=False)
    mattermost_origin: str = ""
    mattermost_email: str = ""
    workspace_root: Path | None = None
    workspace_branches: dict[str, str] = field(default_factory=dict)


def https_origin(value):
    if not value:
        return ""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("服务地址必须是 HTTPS 站点地址。")
    try:
        url = urlsplit(value)
        _ = url.port
    except ValueError:
        raise ValueError("服务地址必须是 HTTPS 站点地址。") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or "\\" in url.netloc
    ):
        raise ValueError("服务地址必须是 HTTPS 站点地址。")
    return f"https://{url.netloc}"


def read_settings(path=CONFIG_PATH):
    try:
        with path.open(encoding="utf-8-sig") as file:
            require_private(path, file)
            data = json.load(file)
    except FileNotFoundError:
        return Settings()
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("网关配置须为有效的 JSON。") from None

    if not isinstance(data, dict):
        raise ValueError("网关配置须为 JSON 对象。")
    op, mm = data.get("openproject", {}), data.get("mattermost", {})
    if not isinstance(op, dict) or not isinstance(mm, dict):
        raise ValueError("openproject 和 mattermost 配置须为 JSON 对象。")
    owner = op.get("user_id")
    key = op.get("api_key", "")
    email = mm.get("email", "")
    root = data.get("workspace_root", "")
    branches = data.get("workspace_branches", {})
    if owner is not None and (type(owner) is not int or owner <= 0):
        raise ValueError("OpenProject user_id 须为正整数。")
    if not isinstance(key, str):
        raise ValueError("OpenProject api_key 须为字符串。")
    if not isinstance(email, str) or (
        email
        and not re.fullmatch(
            r"[\w.%+-]+@[a-z0-9.-]+\.[a-z]{2,}", email, re.IGNORECASE | re.ASCII
        )
    ):
        raise ValueError("Mattermost email 须为有效邮箱地址。")
    if (
        not isinstance(root, str)
        or not isinstance(branches, dict)
        or any(
            not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", name)
            or not isinstance(branch, str)
            or not branch.strip()
            for name, branch in branches.items()
        )
    ):
        raise ValueError("工作区路径或分支配置有误。")
    return Settings(
        openproject_origin=https_origin(op.get("origin", "")),
        openproject_user_id=owner,
        openproject_api_key=key.strip(),
        mattermost_origin=https_origin(mm.get("origin", "")),
        mattermost_email=email.casefold(),
        workspace_root=(path.parent / Path(root).expanduser()).resolve()
        if root
        else None,
        workspace_branches=branches,
    )


SETTINGS = read_settings()
