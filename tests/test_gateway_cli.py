import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, patch

from chat_agent_gateway import __main__ as gateway
from chat_agent_gateway.notify import BridgeError


class GatewayCLITests(unittest.TestCase):
    def test_both_channels_receive_the_explicitly_selected_backend(self):
        backend = AsyncMock()
        with patch.dict(gateway.BACKENDS, {"fixture": backend}):
            for channel in ("lark", "mattermost"):
                with (
                    self.subTest(channel=channel),
                    patch(f"chat_agent_gateway.channels.{channel}.start") as start,
                ):
                    gateway.main(["--backend", "fixture", channel, "run"])
                    self.assertEqual(start.call_args.args[0].mode, "run")
                    self.assertIs(start.call_args.kwargs["run_job"], backend)
        backend.assert_not_called()

    def test_unknown_backend_or_channel_stops_before_any_channel_starts(self):
        with (
            patch("chat_agent_gateway.channels.lark.start") as lark,
            patch("chat_agent_gateway.channels.mattermost.start") as mattermost,
            redirect_stderr(io.StringIO()),
        ):
            for args in (["--backend", "unknown", "lark", "run"], ["unknown", "run"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    gateway.main(args)
                self.assertEqual(error.exception.code, 2)
        lark.assert_not_called()
        mattermost.assert_not_called()

    def test_installed_channel_and_agent_entry_points_can_be_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "gateway_fixture-1.0.dist-info"
            metadata.mkdir()
            (metadata / "entry_points.txt").write_text(
                "[chat_agent_gateway.channels]\nwire-fixture = gateway_plugin_fixture\n"
                "[chat_agent_gateway.backends]\nagent-fixture = gateway_plugin_fixture:run_job\n"
            )
            (root / "gateway_plugin_fixture.py").write_text(
                "import asyncio\n"
                "def configure_parser(parser):\n"
                "    parser.add_argument('--message', required=True)\n"
                "def start(args, *, run_job):\n"
                "    print(asyncio.run(run_job({'summary': args.message}))[0])\n"
                "async def run_job(request, *, progress=None):\n"
                "    return 'agent reply: ' + request['summary'], False\n"
            )
            stream = io.StringIO()
            with (
                patch.object(sys, "path", [directory, *sys.path]),
                patch("sys.stdout", stream),
            ):
                gateway.main(
                    ["--backend", "agent-fixture", "wire-fixture", "--message", "hello"]
                )
            self.assertEqual(stream.getvalue().strip(), "agent reply: hello")
            sys.modules.pop("gateway_plugin_fixture", None)

    def test_plugins_cannot_override_an_existing_backend(self):
        from importlib.metadata import EntryPoint

        entry = EntryPoint("codex", "invalid:load", "chat_agent_gateway.backends")
        with (
            patch("chat_agent_gateway.extensions.entry_points", return_value=[entry]),
            self.assertRaisesRegex(BridgeError, "适配器名称重复"),
        ):
            gateway.main(["lark", "run"])
