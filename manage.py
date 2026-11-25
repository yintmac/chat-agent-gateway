"""Manage the Chat Agent Gateway LaunchAgent on macOS."""

from __future__ import annotations

import argparse
import fcntl
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from chat_agent_gateway.approval import IN_FLIGHT
from chat_agent_gateway.config import CONFIG_PATH
from chat_agent_gateway.paths import PROJECT_DIR, STATE_DIR

LABEL = "local.chat-agent-gateway"
DOMAIN = f"gui/{os.getuid()}"
TARGET = f"{DOMAIN}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
STATE = STATE_DIR / "lark"


def job_status() -> str | None:
    result = subprocess.run(
        ["/bin/launchctl", "print", TARGET], capture_output=True, text=True
    )
    if result.returncode == 113:  # launchd could not find this service.
        return None
    result.check_returncode()
    for line in result.stdout.splitlines():
        if line.strip().startswith("state = "):
            return line.strip().removeprefix("state = ")
    raise RuntimeError("launchctl 未返回任务状态。")


def launchctl(*args: str | Path) -> None:
    subprocess.run(["/bin/launchctl", *args], check=True)


def configuration() -> bytes:
    python = PROJECT_DIR / ".venv/bin/python"
    arguments = [
        str(python),
        "-u",
        "-m",
        "chat_agent_gateway",
        "--backend",
        "codex",
        "lark",
        "run",
    ]
    subprocess.run(
        [
            python,
            "-c",
            "import chat_agent_gateway.__main__\nimport chat_agent_gateway.channels.lark",
        ],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("当前终端找不到 codex。请先确认 Codex CLI 已安装。")
    if not (STATE / "app.json").is_file():
        raise RuntimeError(
            "请先运行 python -m chat_agent_gateway lark pair 绑定 Lark 账号。"
        )
    paths = dict.fromkeys(
        [
            str(Path(codex).parent),
            str(Path.home() / ".local/bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(PROJECT_DIR),
            "RunAtLoad": True,
            "EnvironmentVariables": {
                "HOME": str(Path.home()),
                "CHAT_AGENT_GATEWAY_CONFIG": str(CONFIG_PATH),
                "PATH": os.pathsep.join(paths),
            },
            "StandardOutPath": str(STATE / "service.stdout.log"),
            "StandardErrorPath": str(STATE / "service.stderr.log"),
        }
    )


@contextmanager
def idle_queue() -> Iterator[None]:
    path = STATE / "approvals.sqlite3"
    if not path.exists():
        yield
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        # Hold the writer reservation until the worker has released its process lock.
        db.execute("BEGIN IMMEDIATE")
        task = db.execute(
            "SELECT id, status FROM task WHERE status IN (?, ?, ?) ORDER BY id LIMIT 1",
            IN_FLIGHT,
        ).fetchone()
        if task:
            raise RuntimeError(f"任务 {task[0]} 处于 {task[1]}，暂时不能停服。")
        yield


def stop() -> None:
    with idle_queue():
        launchctl("bootout", TARGET)
        lock = STATE / "lock"
        if lock.exists():
            with lock.open("rb") as file:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("旧进程仍持有服务运行锁。") from None
                        time.sleep(0.1)


def write_plist(data: bytes) -> None:
    PLIST.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=PLIST.parent, delete=False) as file:
        staging = Path(file.name)
        try:
            file.write(data)
            file.flush()
            staging.replace(PLIST)
        finally:
            staging.unlink(missing_ok=True)


def install(loaded: bool) -> None:
    desired = configuration()
    previous = PLIST.read_bytes() if PLIST.exists() else None
    if loaded and previous == desired:
        launchctl("kickstart", TARGET)
        return
    if loaded:
        if previous is None:
            raise RuntimeError(f"请先核对已加载任务的配置文件，预期位置为 {PLIST}。")
        stop()
    try:
        write_plist(desired)
        launchctl("bootstrap", DOMAIN, PLIST)
    except (OSError, subprocess.CalledProcessError):
        if previous is None:
            PLIST.unlink(missing_ok=True)
        else:
            write_plist(previous)
            if loaded:
                launchctl("bootstrap", DOMAIN, PLIST)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("install", "start", "stop", "restart", "status")
    )
    action = parser.parse_args().action
    if action == "status":
        print(f"{LABEL}  {job_status() or '未加载'}")
        return
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "launchd.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        loaded = job_status() is not None
        if action == "install":
            install(loaded)
        elif action == "stop":
            if loaded:
                stop()
        else:
            if not PLIST.is_file():
                raise RuntimeError("请先运行 manage.py install。")
            if action == "start" and loaded:
                launchctl("kickstart", TARGET)
            else:
                if loaded:
                    stop()
                launchctl("bootstrap", DOMAIN, PLIST)


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        subprocess.CalledProcessError,
    ) as error:
        sys.exit(str(error))
