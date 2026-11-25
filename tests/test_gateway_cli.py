import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import AsyncMock, patch

from chat_agent_gateway import __main__ as gateway


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
