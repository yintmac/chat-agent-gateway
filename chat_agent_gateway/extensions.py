"""Discover installed adapters through the Python entry-point registry."""

import re
from importlib.metadata import entry_points

from .notify import BridgeError

CHANNEL_GROUP = "chat_agent_gateway.channels"
BACKEND_GROUP = "chat_agent_gateway.backends"


def installed(group, builtin):
    plugins = {}
    for entry in entry_points(group=group):
        if not re.fullmatch(r"[a-z][a-z0-9-]*", entry.name):
            raise BridgeError(f"无效的适配器名称（{group}）。")
        if entry.name in builtin or entry.name in plugins:
            raise BridgeError(f"适配器名称重复（{group}/{entry.name}）。")
        plugins[entry.name] = entry
    return plugins


def load(entry):
    try:
        return entry.load()
    except Exception as error:
        raise BridgeError(
            f"无法加载适配器 {entry.name}（{type(error).__name__}）。"
        ) from error
