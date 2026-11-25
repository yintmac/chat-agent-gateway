"""Connect a chat channel to an installed agent backend."""

import argparse
import sqlite3
import subprocess
import sys
from importlib import import_module
from pathlib import Path

from .backends.codex import run_codex
from .notify import BridgeError
from .paths import STATE_DIR
from .workflow import RunJob

BACKENDS: dict[str, RunJob] = {"codex": run_codex}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chat-agent-gateway", description=__doc__)
    parser.add_argument(
        "--backend", choices=BACKENDS, default="codex", help="处理对话和任务的执行后端"
    )
    channels = parser.add_subparsers(dest="channel", required=True)
    lark = channels.add_parser("lark", help="Lark 私聊助手和工单提醒")
    lark.add_argument(
        "mode", choices=("pair", "check", "migrate", "bind-mm", "pin-progress", "run")
    )
    mattermost = channels.add_parser("mattermost", help="Mattermost 审批自聊")
    mattermost.add_argument("mode", choices=("preview", "check", "run"))
    mattermost.add_argument("--state-dir", type=Path, default=STATE_DIR)
    mattermost.add_argument("--resume-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if (
        args.channel == "mattermost"
        and args.resume_fd is not None
        and args.mode != "run"
    ):
        mattermost.error("--resume-fd only applies to run")
    channel = import_module(f".channels.{args.channel}", __package__)
    channel.start(args, run_job=BACKENDS[args.backend])


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
    except (
        BridgeError,
        OSError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        sys.exit(f"Chat Agent Gateway 已停止。{error}")
