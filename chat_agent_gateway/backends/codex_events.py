"""Project Codex JSONL into observable milestones and local, bounded details."""

import json
import re
import shlex
from pathlib import Path

from ..notify import BridgeError

COMMAND_ACTIONS = {
    "cat": "读取文件",
    "sed": "检查文件内容",
    "head": "查看文件开头",
    "tail": "查看文件末尾",
    "rg": "检索代码",
    "find": "查找文件",
    "git": "核对版本库",
    "pytest": "运行测试",
    "ruff": "运行代码检查",
    "mypy": "检查类型",
    "go": "运行 Go 命令",
    "cargo": "运行 Cargo 命令",
    "pnpm": "运行项目脚本",
    "npm": "运行项目脚本",
    "mvn": "运行 Maven 命令",
    "gradle": "运行 Gradle 命令",
    "python": "运行 Python 命令",
    "python3": "运行 Python 命令",
}
GIT_ACTIONS = {
    "status": "查看工作区状态",
    "diff": "核对代码改动",
    "log": "查看提交历史",
    "show": "查看提交或文件版本",
    "ls-files": "核对跟踪文件",
    "worktree": "核对 Git 工作树",
    "branch": "核对 Git 分支",
}
PROJECT_ACTIONS = {
    "test": "运行测试",
    "pytest": "运行测试",
    "vitest": "运行测试",
    "unittest": "运行测试",
    "build": "构建项目",
    "check": "运行代码检查",
    "lint": "运行代码检查",
    "ruff": "运行代码检查",
    "format": "检查或整理代码格式",
    "typecheck": "检查类型",
    "mypy": "检查类型",
}
HIDDEN_NAMES = (
    "secret",
    "credential",
    "password",
    "token",
    "apikey",
    "api_key",
    "private",
    ".env",
    ".pem",
    ".key",
    "id_rsa",
    "id_ed25519",
)
FILE_NAME = re.compile(r"[\w][\w ./+-]{0,119}")


def display_path(value, directory):
    if directory is None or not isinstance(value, str) or not value or len(value) > 512:
        return None
    try:
        path = Path(value)
        root = directory.resolve()
        relative = (root / path).resolve().relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return None
    label = relative.as_posix()
    if not FILE_NAME.fullmatch(label) or any(
        part.startswith(".") or any(word in part.lower() for word in HIDDEN_NAMES)
        for part in relative.parts
    ):
        return None
    return label


def command_args(command):
    if not isinstance(command, str) or len(command) > 4096:
        return []
    try:
        args = shlex.split(command)
        if (
            len(args) == 3
            and args[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh"}
            and args[1] in {"-c", "-lc"}
        ):
            args = shlex.split(args[2])
    except ValueError:
        return []
    return args


def command_step(item, finished, directory):
    args = command_args(item.get("command"))
    executable = args[0].rsplit("/", 1)[-1] if args else ""
    tool = executable if executable in COMMAND_ACTIONS else ""
    action = COMMAND_ACTIONS.get(tool, "运行命令")
    subcommand = args[1] if len(args) > 1 else ""
    if tool == "git":
        action = GIT_ACTIONS.get(subcommand, action)
    elif tool in {"go", "cargo", "npm", "pnpm", "mvn", "gradle"}:
        index = 2 if subcommand in {"run", "exec"} else 1
        subcommand = args[index] if len(args) > index else ""
        if subcommand in PROJECT_ACTIONS:
            action = PROJECT_ACTIONS[subcommand]
            tool += " " + subcommand
    elif tool in {"python", "python3"} and subcommand == "-m" and len(args) > 2:
        module = args[2]
        if module in PROJECT_ACTIONS:
            action = PROJECT_ACTIONS[module]
            tool += f" -m {module}"
    elif tool == "ruff" and subcommand == "format":
        action = PROJECT_ACTIONS[subcommand]
        tool += " format"

    detail = [tool] if tool else []
    if executable == "cat" and len(args) == 2:
        path = display_path(args[1], directory)
        if path:
            detail.append(path)
    code = item.get("exit_code")
    if finished and type(code) is int:
        detail.append(f"退出码 {code}")
    if finished:
        label = (
            f"{action} · {'非零退出' if type(code) is int and code != 0 else '已结束'}"
        )
    else:
        label = action
    return label, "  ·  ".join(detail) or None


def change_step(item, finished, directory):
    label = "正在修改文件"
    if finished:
        label = "文件修改失败" if item.get("status") == "failed" else "文件变更已结束"
    changes = item.get("changes")
    if not isinstance(changes, list) or not changes:
        return label, None
    visible = []
    verbs = {"add": "新增", "delete": "删除", "update": "修改"}
    for change in changes[:24]:
        path = display_path(change.get("path"), directory)
        if path:
            verb = verbs.get(change.get("kind"), "变更")
            visible.append(f"{verb} {path}")
        if len(visible) == 3:
            break
    return label, "  ·  ".join([f"{len(changes)} 项变更", *visible])


def progress_step(event, directory):
    kind = event.get("type")
    if kind == "thread.started":
        return "已建立 Codex 会话", None
    if kind == "turn.started":
        return "Codex 正在处理", None
    if kind in ("error", "turn.failed"):
        return "后端报告异常", None
    if kind not in ("item.started", "item.updated", "item.completed"):
        return None
    item = event.get("item") or {}
    category = item.get("type")
    if category == "todo_list":
        items = item.get("items") or []
        if items:
            done = sum(i.get("completed") is True for i in items)
            return f"计划进度 {done}/{len(items)}", None
    if kind == "item.updated":
        return None
    finished = kind == "item.completed"
    if category == "command_execution":
        return command_step(item, finished, directory)
    if category == "file_change":
        return change_step(item, finished, directory)
    if category in ("mcp_tool_call", "dynamic_tool_call"):
        return ("工具调用已结束" if finished else "正在调用工具"), None
    if category == "web_search":
        return ("检索已结束" if finished else "正在检索"), None
    return None


async def read_events(stream, progress, *, directory=None):
    # Some command-completion events contain large output blobs. The final
    # response is written independently by Codex; never buffer an unbounded line.
    line = bytearray()
    oversized = False

    def record():
        if oversized:
            step = ("已收到大型执行事件，详情未展示", None)
        elif line.strip():
            try:
                step = progress_step(json.loads(line), directory)
            except (ValueError, AttributeError, TypeError):
                raise BridgeError("无法识别 Codex 返回的进度数据。") from None
        else:
            return
        if step:
            label, detail = step
            if detail:
                progress(label, detail=detail)
            else:
                progress(label)

    while chunk := await stream.read(65536):
        if progress is None:
            continue
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not oversized:
                if len(line) + len(part) > 1024 * 1024:
                    oversized = True
                    line.clear()
                else:
                    line.extend(part)
            if index < len(parts) - 1:
                record()
                line.clear()
                oversized = False
    if progress and (line or oversized):
        record()
