"""Connect a chat channel to an installed agent backend."""

import argparse
import sqlite3
import subprocess
import sys
from importlib import import_module

from .backends.codex import run_codex
from .extensions import BACKEND_GROUP, CHANNEL_GROUP, installed, load
from .notify import BridgeError
from .workflow import RunJob

BACKENDS: dict[str, RunJob] = {"codex": run_codex}
CHANNELS = {"lark", "mattermost"}


def main(argv=None):
    external_backends = installed(BACKEND_GROUP, BACKENDS)
    external_channels = installed(CHANNEL_GROUP, CHANNELS)
    parser = argparse.ArgumentParser(prog="chat-agent-gateway")
    parser.add_argument(
        "--backend",
        choices=sorted(BACKENDS.keys() | external_backends.keys()),
        default="codex",
        help="处理对话和任务的执行后端",
    )
    parser.add_argument("channel", choices=sorted(CHANNELS | external_channels.keys()))
    parser.add_argument(
        "channel_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
    )
    scope = parser.parse_args(argv)

    name = scope.channel
    channel = (
        import_module(f".channels.{name}", __package__)
        if name in CHANNELS
        else load(external_channels[name])
    )
    if not callable(getattr(channel, "configure_parser", None)) or not callable(
        getattr(channel, "start", None)
    ):
        raise BridgeError(f"渠道 {name} 须提供 configure_parser 和 start。")
    channel_parser = argparse.ArgumentParser(prog=f"{parser.prog} {name}")
    channel.configure_parser(channel_parser)
    args = channel_parser.parse_args(scope.channel_args)
    args.backend = scope.backend
    backend = (
        BACKENDS[scope.backend]
        if scope.backend in BACKENDS
        else load(external_backends[scope.backend])
    )
    if not callable(backend):
        raise BridgeError(f"执行后端 {scope.backend} 须提供可调用的 run_job。")
    channel.start(args, run_job=backend)


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
