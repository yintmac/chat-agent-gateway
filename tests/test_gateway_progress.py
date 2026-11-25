import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from chat_agent_gateway.approval import Approvals
from chat_agent_gateway.backends.codex import codex_output
from chat_agent_gateway.backends.codex_events import read_events
from chat_agent_gateway.conversations import Conversations
from chat_agent_gateway.local import acquire_lock
from chat_agent_gateway.monitor import MAX_EVENTS, Monitor, snapshot, status_text
from chat_agent_gateway.notify import BridgeError


class CodexStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_and_private_events_preserve_subsequent_steps(self):
        events = [
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'rg private-argument .'",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "aggregated_output": "x" * 1_100_000,
                },
            },
            {
                "type": "item.completed",
                "item": {"type": "reasoning", "text": "private"},
            },
            {
                "type": "item.updated",
                "item": {
                    "type": "todo_list",
                    "items": [{"completed": True}, {"completed": False}],
                },
            },
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "exit_code": 1},
            },
        ]
        stream = asyncio.StreamReader()
        stream.feed_data("\n".join(map(json.dumps, events)).encode())
        stream.feed_eof()
        steps = []

        def record(label, *, detail=None):
            steps.append((label, detail))

        await read_events(stream, record)
        self.assertEqual(
            steps,
            [
                ("检索代码", "rg"),
                ("已收到大型执行事件，详情未展示", None),
                ("计划进度 1/2", None),
                ("运行命令 · 非零退出", "退出码 1"),
            ],
        )

    async def test_reports_specific_checks_and_worktree_paths_without_raw_payloads(
        self,
    ):
        events = [
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'python3 -m unittest discover -s tests'",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "ruff check .",
                    "aggregated_output": "raw command output",
                    "exit_code": 0,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "status": "completed",
                    "changes": [
                        {"path": str(self.path / "src" / "queue.py"), "kind": "update"},
                        {"path": "../outside.py", "kind": "add"},
                        {"path": ".env", "kind": "add"},
                        {"path": "config/credentials.json", "kind": "delete"},
                    ],
                },
            },
        ]
        stream = asyncio.StreamReader()
        stream.feed_data("\n".join(map(json.dumps, events)).encode())
        stream.feed_eof()
        report = Mock()
        await read_events(stream, report, directory=self.path)
        self.assertEqual(
            [(call.args[0], call.kwargs["detail"]) for call in report.call_args_list],
            [
                ("运行测试", "python3 -m unittest"),
                ("运行代码检查 · 已结束", "ruff  ·  退出码 0"),
                ("文件变更已结束", "4 项变更  ·  修改 src/queue.py"),
            ],
        )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.executable = self.path / "codex-fixture.py"
        executable = patch(
            "chat_agent_gateway.backends.codex.codex_command",
            return_value=(sys.executable, str(self.executable)),
        )
        executable.start()
        self.addCleanup(executable.stop)

    def fixture(self, script):
        self.executable.write_text(
            "import sys, time\nfrom pathlib import Path\n"
            "response = Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
            + script,
            encoding="utf-8",
        )

    async def test_final_response_is_independent_from_events_and_preserves_exact_limit(
        self,
    ):
        self.fixture(
            "assert sys.stdin.read() == 'first\\nsecond'\n"
            'print(\'{"type":"turn.started"}\',flush=True)\n'
            "response.write_text('界'*3500+'\\n', encoding='utf-8')\n"
        )
        labels = []
        answer = await codex_output(
            iter(["first\n", "second"]), self.path, "read-only", progress=labels.append
        )
        self.assertEqual(answer, "界" * 3500)
        self.assertEqual(labels, ["Codex 正在处理"])

    async def test_backend_failures_are_explicit_without_exposing_tool_output(self):
        for script, error in (
            ("sys.exit(9)", "状态码 9"),
            ("pass", "没有返回最终回复"),
            ("response.write_text('x'*3501, encoding='utf-8')", "超过 3500"),
            ("print('private-output')", "无法识别"),
        ):
            with self.subTest(error=error):
                self.fixture(script)
                with self.assertRaisesRegex(BridgeError, error):
                    await codex_output(
                        "input", self.path, "read-only", progress=lambda _: None
                    )

    async def test_cancellation_reaps_the_process_while_both_pipes_are_active(self):
        self.fixture('print(\'{"type":"turn.started"}\',flush=True)\ntime.sleep(30)\n')
        started = asyncio.Event()
        processes = []
        create_original = asyncio.create_subprocess_exec

        async def capture_process(*args, **kwargs):
            process = await create_original(*args, **kwargs)
            processes.append(process)
            return process

        with patch(
            "chat_agent_gateway.backends.codex.asyncio.create_subprocess_exec",
            side_effect=capture_process,
        ):
            task = asyncio.create_task(
                codex_output(
                    "a" * 1_000_000,
                    self.path,
                    "read-only",
                    progress=lambda _: started.set(),
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 8)
            finally:
                task.cancel()
                (result,) = await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True), 8
                )
                self.assertIsInstance(result, asyncio.CancelledError)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].returncode)


class MonitorTests(unittest.TestCase):
    def test_read_only_snapshot_tracks_lock_queue_events_and_interrupted_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertFalse(snapshot(state)["online"])
            self.assertEqual(list(state.iterdir()), [])
            queue = Approvals(state / "approvals.sqlite3", "owner", "dm")
            dialogs = Conversations(queue)
            first = dialogs.receive("dm", "first", "检查第一个需求", [])
            second = dialogs.receive("dm", "next", "后续任务", [])
            with (state / "worker.lock").open("a+b") as lock:
                acquire_lock(lock, blocking=True)
                queue.claim(first)
                for i in range(MAX_EVENTS + 3):
                    dialogs.progress(first, f"步骤 {i}")
                view = snapshot(state)
                self.assertTrue(view["online"])
                self.assertEqual((view["active"], view["queued"]), (1, 1))
                self.assertEqual([r["id"] for r in view["tasks"]], [first, second])
                self.assertEqual(len(view["events"]), MAX_EVENTS)
                self.assertEqual(view["total"], MAX_EVENTS + 5)
                self.assertEqual(snapshot(state, second)["total"], 1)
                self.assertEqual(view["events"][-1][1], f"步骤 {MAX_EVENTS + 2}")
            offline = snapshot(state)
            self.assertEqual(
                (offline["online"], offline["active"], offline["interrupted"]),
                (False, 0, 1),
            )
            self.assertIn("中断", status_text(offline["tasks"][0], False))
            queue.finish(first, "需要提供信息", blocked=True)
            queue.start_result(first)
            queue.result_posted(first, "answer")
            done = snapshot(state, first)
            task = next(item for item in done["tasks"] if item["id"] == first)
            self.assertIsNotNone(task["ended"])
            self.assertEqual(status_text(task, False), "需要核对")
            self.assertEqual(
                [label for _, label, _ in done["events"][-3:]],
                ["需要你核对", "正在发送回复", "回复已送达"],
            )

    def test_old_timeline_survives_detail_migration_and_result_preview_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue = Approvals(state / "approvals.sqlite3", "owner", "dm")
            dialogs = Conversations(queue)
            task = dialogs.receive("dm", "request", "检查代码", [])
            queue.claim(task)
            dialogs.progress(task, "旧记录")
            with closing(queue.connect()) as db, db:
                db.execute("ALTER TABLE run_event DROP COLUMN detail")
            before = snapshot(state, task)
            self.assertEqual(before["events"][-1][1:], ("旧记录", None))
            dialogs = Conversations(queue)
            dialogs.progress(task, "运行验证", detail="python -m unittest")
            queue.finish(task, "结果：" + "甲" * 300, blocked=False)
            after = snapshot(state, task)
            self.assertIn(
                ("旧记录", None),
                [(label, detail) for _, label, detail in after["events"]],
            )
            self.assertEqual(
                after["events"][-2][1:], ("运行验证", "python -m unittest")
            )
            self.assertEqual(len(after["result_preview"]), 221)
            self.assertTrue(after["result_preview"].endswith("…"))

    def test_lock_contention_clears_stale_display_and_preserves_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue = Approvals(state / "approvals.sqlite3", "owner", "dm")
            Conversations(queue)
            ui = Mock(state=state, selected=None)
            with closing(queue.connect()) as db:
                db.execute("BEGIN EXCLUSIVE")
                with self.assertRaises(sqlite3.OperationalError):
                    snapshot(state)
                Monitor.update_view(ui)
                ui.render.assert_not_called()
                ui.log.delete.assert_called_once_with("1.0", "end")
            before = queue.path.read_bytes()
            Monitor.update_view(ui)
            ui.render.assert_called_once()
            self.assertEqual(queue.path.read_bytes(), before)
