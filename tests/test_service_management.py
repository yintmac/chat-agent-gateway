import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import manage
from chat_agent_gateway.approval import IN_FLIGHT
from chat_agent_gateway.config import CONFIG_PATH
from chat_agent_gateway.paths import PROJECT_DIR, STATE_DIR
from chat_agent_gateway.channels.lark import STATE_DIR as LARK_STATE


@unittest.skipUnless(sys.platform == "darwin", "LaunchAgent is macOS-only")
class ServiceManagementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.plist = self.state / "agents" / "lark.plist"
        for key, value in (("STATE", self.state), ("PLIST", self.plist)):
            replacement = patch.object(manage, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.database = self.state / "approvals.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, status TEXT)")
            db.execute("INSERT INTO task VALUES (7, 'awaiting')")

    def test_configuration_targets_project_and_existing_approval_state(self):
        self.assertEqual(PROJECT_DIR, Path(__file__).resolve().parents[1])
        expected = Path.home() / "Library/Application Support/Chat Agent Gateway"
        self.assertEqual(STATE_DIR, expected)
        self.assertEqual(LARK_STATE, expected / "lark")
        (self.state / "app.json").touch()
        with (
            patch.object(manage.shutil, "which", return_value="/fixture/codex"),
            patch.object(manage.subprocess, "run"),
        ):
            plist = plistlib.loads(manage.configuration())
        self.assertEqual(plist["Label"], "local.chat-agent-gateway")
        self.assertEqual(plist["WorkingDirectory"], str(PROJECT_DIR))
        self.assertEqual(
            plist["EnvironmentVariables"]["CHAT_AGENT_GATEWAY_CONFIG"], str(CONFIG_PATH)
        )
        self.assertEqual(
            plist["ProgramArguments"],
            [
                str(PROJECT_DIR / ".venv/bin/python"),
                "-u",
                "-m",
                "chat_agent_gateway",
                "--backend",
                "codex",
                "lark",
                "run",
            ],
        )
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(
            plist["StandardOutPath"], str(self.state / "service.stdout.log")
        )

    def test_stop_refuses_in_flight_work_before_unloading(self):
        with (
            closing(sqlite3.connect(self.database)) as db,
            patch.object(manage, "launchctl") as ctl,
        ):
            for status in IN_FLIGHT:
                with self.subTest(status=status):
                    with db:
                        db.execute("UPDATE task SET status = ?", (status,))
                    with self.assertRaisesRegex(RuntimeError, status):
                        manage.stop()
            ctl.assert_not_called()

    def test_stop_holds_queue_until_worker_releases_its_lock(self):
        (self.state / "lock").touch()
        with closing(sqlite3.connect(self.database, timeout=0)) as other:

            def bootout(*args):
                self.assertEqual(args, ("bootout", manage.TARGET))
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")

            with patch.object(manage, "launchctl", side_effect=bootout):
                manage.stop()
            other.execute("BEGIN IMMEDIATE")
            self.assertEqual(
                other.execute("SELECT * FROM task").fetchall(), [(7, "awaiting")]
            )

    def test_install_restores_loaded_job_if_launchd_rejects_replacement(self):
        manage.write_plist(b"previous")
        observed = []

        def bootstrap(*args):
            self.assertEqual(args, ("bootstrap", manage.DOMAIN, self.plist))
            observed.append(self.plist.read_bytes())
            if len(observed) == 1:
                raise subprocess.CalledProcessError(5, "launchctl")

        with (
            patch.object(manage, "configuration", return_value=b"replacement"),
            patch.object(manage, "stop") as stop,
            patch.object(manage, "launchctl", side_effect=bootstrap),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            manage.install(loaded=True)
        stop.assert_called_once_with()
        self.assertEqual(observed, [b"replacement", b"previous"])
        self.assertEqual(self.plist.read_bytes(), b"previous")

    def test_reinstall_of_loaded_job_does_not_stop_it(self):
        manage.write_plist(b"current")
        with (
            patch.object(manage, "configuration", return_value=b"current"),
            patch.object(manage, "stop") as stop,
            patch.object(manage, "launchctl") as ctl,
        ):
            manage.install(loaded=True)
        stop.assert_not_called()
        ctl.assert_called_once_with("kickstart", manage.TARGET)
