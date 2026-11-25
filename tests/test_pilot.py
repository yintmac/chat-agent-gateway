import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock, call, patch

from chat_agent_gateway.approval import Approvals, Reply, Request
from chat_agent_gateway.notify import (
    MATTERMOST,
    OPENPROJECT,
    RECIPIENT_EMAIL,
    ApiError,
    BridgeError,
)
from chat_agent_gateway.channels.mattermost import (
    MattermostChannel,
    ReloadRequested,
    mattermost_request,
    mattermost_session,
    poll,
    run,
    sign_in_mattermost,
    watch_code,
)

from chat_agent_gateway.backends.codex import run_codex
from chat_agent_gateway.workspaces import mentioned_repository, workspace_for

ME = "a" * 26
INBOX = "b" * 26
COLLEAGUE = "c" * 26
CHANNEL = "d" * 26
ORIGINAL = "e" * 26
APPROVAL = "f" * 26
RESULT = "g" * 26


def event(author, channel, post_id, message, root_id="", mentions=()):
    return {
        "event": "posted",
        "data": {
            "post": json.dumps(
                {
                    "user_id": author,
                    "channel_id": channel,
                    "id": post_id,
                    "root_id": root_id,
                    "message": message,
                }
            ),
            "mentions": json.dumps(mentions),
        },
    }


class FakeMattermost:
    def __init__(self, fail_post=False):
        self.posts = []
        self.fail_post = fail_post

    def request(self, path, body):
        assert path == "/api/v4/posts"
        if self.fail_post:
            raise BridgeError("connection lost")
        self.posts.append(body)
        return {
            "id": APPROVAL if len(self.posts) == 1 else RESULT,
            "channel_id": body["channel_id"],
            "root_id": body.get("root_id", ""),
        }


class PilotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.ledger = Approvals(self.path / "db.sqlite", ME, INBOX)
        self.request = Request(
            "op:1", "ticket:12345", "source", "request", CHANNEL, ORIGINAL
        )

    def test_unapproved_and_replayed_jobs_cannot_run(self):
        task = self.ledger.enqueue(self.request)
        self.assertEqual(self.ledger.enqueue(self.request), task)
        self.assertEqual(
            self.ledger.enqueue(replace(self.request, event_id="op:2")), task
        )
        self.assertEqual(
            self.ledger.enqueue(
                replace(self.request, event_id="op:3", update="状态变化")
            ),
            task,
        )
        self.assertIsNone(self.ledger.claim(task))
        self.ledger.start_approval(task)
        self.ledger.approval_posted(task, APPROVAL)
        for author, channel, root in (
            (COLLEAGUE, INBOX, APPROVAL),
            (ME, CHANNEL, APPROVAL),
            (ME, INBOX, ORIGINAL),
        ):
            self.assertIsNone(
                self.ledger.accept_reply(Reply(author, channel, root, "确认"))
            )
        self.assertEqual(
            self.ledger.accept_reply(Reply(ME, INBOX, APPROVAL, "确认")), task
        )
        self.assertIsNotNone(self.ledger.claim(task))
        reopened = Approvals(self.path / "db.sqlite", ME, INBOX)
        self.assertIsNone(reopened.claim(task))
        self.assertEqual(reopened.tasks_in("running"), [task])

    def test_approved_recipient_and_workspace_are_pinned(self):
        task = self.ledger.enqueue(self.request)
        self.ledger.start_approval(task)
        self.ledger.approval_posted(task, APPROVAL)
        reply = Reply(ME, INBOX, APPROVAL, "确认 sample-app @colleague")
        self.ledger.accept_reply(reply, workspace="sample-app", recipient=COLLEAGUE)
        self.assertEqual(self.ledger.claim(task)["workspace"], "sample-app")
        self.ledger.finish(task, "已完成", blocked=False)
        self.assertEqual(
            self.ledger.start_result(task),
            (CHANNEL, ORIGINAL, COLLEAGUE, "已完成", "ticket:12345"),
        )
        self.assertIsNone(self.ledger.start_result(task))

    def test_blocker_stays_in_approval_inbox(self):
        task = self.ledger.enqueue(self.request)
        self.ledger.start_approval(task)
        self.ledger.approval_posted(task, APPROVAL)
        self.ledger.accept_reply(Reply(ME, INBOX, APPROVAL, "确认"))
        self.ledger.claim(task)
        self.ledger.finish(task, "被阻塞", blocked=True)
        self.assertEqual(
            self.ledger.start_result(task), (INBOX, APPROVAL, None, "被阻塞", None)
        )

    def test_actual_event_shapes_flow_through_confirmation_and_original_thread(self):
        mm = FakeMattermost()
        calls = []

        async def run_job(request):
            calls.append(request)
            return "看完了，没改代码。", False

        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=run_job
        )
        incoming = event(
            COLLEAGUE, CHANNEL, ORIGINAL, "@operator 帮我看一下", mentions=(ME,)
        )
        pilot.receive(incoming)
        pilot.receive(incoming)
        pilot.deliver()
        asyncio.run(pilot.execute())
        self.assertEqual(calls, [])
        self.assertEqual(len(mm.posts), 1)
        self.assertEqual(mm.posts[0]["channel_id"], INBOX)
        self.assertIn("在自聊底部直接发指令", mm.posts[0]["message"])
        self.assertIn("`确认 审批1`", mm.posts[0]["message"])
        self.assertIn("会先在配置的工作区查仓库", mm.posts[0]["message"])

        pilot.receive(event(ME, INBOX, "h" * 26, "确认", APPROVAL))
        asyncio.run(pilot.execute())
        pilot.deliver()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["auto_route"])
        self.assertEqual(
            mm.posts[1],
            {
                "channel_id": CHANNEL,
                "root_id": ORIGINAL,
                "message": "看完了，没改代码。",
            },
        )

    def test_ambiguous_post_is_not_resent(self):
        task = self.ledger.enqueue(self.request)
        mm = FakeMattermost(fail_post=True)
        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=AsyncMock()
        )
        with self.assertRaisesRegex(BridgeError, "connection lost"):
            pilot.deliver()
        pilot.deliver()
        self.assertEqual(mm.posts, [])
        self.assertEqual(self.ledger.tasks_in("posting"), [task])

    def test_owner_can_cancel_only_in_the_approval_thread(self):
        task = self.ledger.enqueue(self.request)
        pilot = MattermostChannel(
            None,
            FakeMattermost(),
            self.ledger,
            self.path / "cursor",
            "operator",
            run_job=AsyncMock(),
        )
        pilot.deliver()
        pilot.receive(event(ME, INBOX, "h" * 26, "取消"))
        self.assertEqual(self.ledger.tasks_in("awaiting"), [task])

        pilot.receive(event(ME, INBOX, "i" * 26, "取消", APPROVAL))
        reopened = Approvals(self.path / "db.sqlite", ME, INBOX)
        self.assertEqual(reopened.tasks_in("cancelled"), [task])
        self.assertIsNone(reopened.claim(task))

    def test_self_chat_command_without_a_thread_explains_why_it_was_ignored(self):
        pilot = MattermostChannel(
            None,
            FakeMattermost(),
            self.ledger,
            self.path / "cursor",
            "operator",
            run_job=AsyncMock(),
        )
        with patch("builtins.print") as log:
            pilot.receive(event(ME, INBOX, "h" * 26, "确认"))
        self.assertIn("请带上审批号", log.call_args.args[0])
        self.assertEqual(self.ledger.tasks_in("approved"), [])

    def test_numbered_self_chat_commands_select_only_the_exact_pending_approval(self):
        first = self.ledger.enqueue(self.request)
        second = self.ledger.enqueue(
            replace(self.request, event_id="op:2", topic="ticket:23456")
        )
        pilot = MattermostChannel(
            None,
            FakeMattermost(),
            self.ledger,
            self.path / "cursor",
            "operator",
            run_job=AsyncMock(),
        )
        pilot.deliver()
        command = f"确认 审批{second}"
        pilot.receive(event(COLLEAGUE, INBOX, "h" * 26, command))
        pilot.receive(event(ME, CHANNEL, "i" * 26, command))
        self.assertEqual(self.ledger.tasks_in("awaiting"), [first, second])

        pilot.receive(event(ME, INBOX, "j" * 26, command))
        pilot.receive(event(ME, INBOX, "j" * 26, command))
        pilot.receive(event(ME, INBOX, "k" * 26, f"取消 审批{first}"))
        self.assertEqual(self.ledger.tasks_in("approved"), [second])
        self.assertEqual(self.ledger.tasks_in("cancelled"), [first])

    def test_result_in_self_chat_is_visible_as_a_top_level_message(self):
        task = self.ledger.enqueue(replace(self.request, channel_id="", root_id=""))
        mm = FakeMattermost()
        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=AsyncMock()
        )
        pilot.deliver()
        pilot.receive(event(ME, INBOX, "h" * 26, f"确认 审批{task}"))
        self.ledger.claim(task)
        self.ledger.finish(task, "检查完成。", blocked=False)
        pilot.deliver()
        self.assertEqual(mm.posts[1]["channel_id"], INBOX)
        self.assertNotIn("root_id", mm.posts[1])
        self.assertTrue(
            mm.posts[1]["message"].startswith(f"**审批{task}**\n\n检查完成。")
        )

    def test_known_repository_is_displayed_and_simple_confirmation_uses_it(self):
        request = replace(self.request, summary="Please handle this in example-web")
        task = self.ledger.enqueue(request)
        mm = FakeMattermost()
        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=AsyncMock()
        )
        with patch(
            "chat_agent_gateway.channels.mattermost.available_repositories",
            return_value=["example-web", "example-worker"],
        ):
            pilot.deliver()
        self.assertIn(
            "发送 `确认 审批1` 就在 `example-web` 处理", mm.posts[0]["message"]
        )
        pilot.receive(event(ME, INBOX, "h" * 26, "确认", APPROVAL))
        self.assertEqual(self.ledger.claim(task)["workspace"], "example-web")

    def test_explicit_repo_names_require_one_exact_match(self):
        candidates = ["repo-a", "repo-b", "repo-a-docs"]
        self.assertEqual(mentioned_repository("org/repo-a.git", candidates), "repo-a")
        self.assertEqual(mentioned_repository("repo-a-docs", candidates), "repo-a-docs")
        self.assertIsNone(mentioned_repository("repo-a-legacy", candidates))
        self.assertIsNone(mentioned_repository("repo-a and repo-b", candidates))

    def test_restart_recovers_cancellation_from_the_original_mattermost_thread(self):
        task = self.ledger.enqueue(self.request)
        self.ledger.start_approval(task)
        self.ledger.approval_posted(task, APPROVAL)

        def record(author, post_id, message, root=""):
            return json.loads(
                event(author, INBOX, post_id, message, root)["data"]["post"]
            )

        mm = Mock()
        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=AsyncMock()
        )
        mm.request.return_value = {
            "posts": {
                APPROVAL: record(ME, APPROVAL, "测试审批"),
                "h" * 26: record(ME, "h" * 26, "确认", APPROVAL),
                "i" * 26: record(COLLEAGUE, "i" * 26, "取消", APPROVAL),
            }
        }
        pilot.recover_cancellations()
        self.assertEqual(self.ledger.tasks_in("awaiting"), [task])
        self.assertEqual(self.ledger.tasks_in("approved"), [])

        mm.request.return_value["posts"]["j" * 26] = record(
            ME, "j" * 26, "取消", APPROVAL
        )
        with patch("builtins.print"):
            pilot.recover_cancellations()
        mm.request.assert_called_with(f"/api/v4/posts/{APPROVAL}/thread?perPage=0")
        self.assertEqual(self.ledger.tasks_in("cancelled"), [task])

    def test_openproject_read_error_does_not_exit_the_poll_loop(self):
        mm = Mock()
        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=AsyncMock()
        )
        with (
            patch.object(
                pilot,
                "poll_openproject",
                side_effect=(BridgeError(f"{OPENPROJECT}: response error"), None),
            ) as read,
            patch(
                "chat_agent_gateway.channels.mattermost.asyncio.sleep",
                new=AsyncMock(side_effect=(None, asyncio.CancelledError)),
            ) as pause,
            patch("builtins.print") as log,
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(poll(pilot))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(
            mm.request.call_args_list,
            [call("/api/v4/users/me"), call("/api/v4/users/me")],
        )
        self.assertEqual(pause.await_args_list, [call(60), call(60)])
        self.assertIn("Mattermost 继续监听", log.call_args.args[0])

    def test_mattermost_ignores_posts_without_a_mention(self):
        record = json.loads(
            event(COLLEAGUE, CHANNEL, ORIGINAL, "只是讨论")["data"]["post"]
        )
        self.assertIsNone(mattermost_request(record, [], ME, "operator", None))
        record["message"] = "@channel 帮我看一下"
        self.assertIsNone(mattermost_request(record, [ME], ME, "operator", None))

    def test_worktree_selection_refuses_path_escape_and_uses_configured_branch(self):
        repo = self.path / "sample-app"
        repo.mkdir()
        (repo / ".git").mkdir()
        worktree = self.path / "active"
        worktree.mkdir()
        listing = f"worktree {repo}\nbranch refs/heads/other\n\nworktree {worktree}\nbranch refs/heads/review"
        with (
            patch.dict(
                "chat_agent_gateway.workspaces.BRANCHES", {"sample-app": "review"}
            ),
            patch(
                "chat_agent_gateway.workspaces.git", side_effect=[str(repo), listing]
            ),
        ):
            self.assertEqual(workspace_for("sample-app", self.path), worktree.resolve())
        with self.assertRaisesRegex(BridgeError, "目录名"):
            workspace_for("../outside", self.path)

    def test_expired_mattermost_session_requires_interactive_login(self):
        mm = Mock()
        mm.request.side_effect = ApiError(MATTERMOST, 401)
        pilot = MattermostChannel(
            None, mm, self.ledger, self.path / "cursor", "operator", run_job=AsyncMock()
        )

        async def ready(_, connected):
            connected.set()
            await asyncio.Future()

        with patch("chat_agent_gateway.channels.mattermost.listen", ready):
            with self.assertRaisesRegex(BridgeError, "会话已失效，请重新运行并登录"):
                asyncio.run(run(pilot))
        mm.request.assert_called_once_with("/api/v4/users/me")


class CodexRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve() / "alpha"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(self.repo)], check=True, capture_output=True
        )
        (self.repo / "README.md").write_text("Repository handles this fixture.")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "README.md"],
            check=True,
            capture_output=True,
        )
        self.request = {
            "workspace": None,
            "auto_route": True,
            "source": "fixture",
            "summary": "Handle the fixture",
        }

    def run_with_decision(self, decision, *, progress=None):
        output = AsyncMock(side_effect=[json.dumps(decision), "已处理。"])
        with (
            patch(
                "chat_agent_gateway.backends.codex.available_repositories",
                return_value=["alpha"],
            ),
            patch(
                "chat_agent_gateway.backends.codex.workspace_for",
                return_value=self.repo,
            ),
            patch("chat_agent_gateway.backends.codex.codex_output", output),
            patch("builtins.print"),
        ):
            result = asyncio.run(run_codex(self.request, progress=progress))
        return result, output

    def test_confirm_routes_and_works_only_in_selected_repo(self):
        for mode, sandbox in (("modify", "workspace-write"), ("inspect", "read-only")):
            with self.subTest(mode=mode):
                progress = Mock()
                (answer, blocked), output = self.run_with_decision(
                    {
                        "mode": mode,
                        "repository": "alpha",
                        "evidence_file": "README.md",
                        "reason": "文件和工单匹配。",
                    },
                    progress=progress,
                )
                self.assertFalse(blocked)
                self.assertEqual(answer, "已处理。")
                route, work = output.await_args_list
                self.assertEqual(route.args[2], "read-only")
                self.assertIn("schema", route.kwargs)
                self.assertEqual(work.args[1:], (self.repo, sandbox))
                self.assertIn("alpha", progress.call_args.args[0])
                self.assertEqual(
                    progress.call_args.kwargs["detail"], "定位依据 README.md"
                )

    def test_ambiguous_or_non_code_request_never_opens_a_write_turn(self):
        for mode in ("unclear", "analysis"):
            with self.subTest(mode=mode):
                progress = Mock()
                (_, blocked), output = self.run_with_decision(
                    {
                        "mode": mode,
                        "repository": None,
                        "evidence_file": None,
                        "reason": "需要补充上下文。",
                    },
                    progress=progress,
                )
                self.assertEqual(blocked, mode == "unclear")
                self.assertEqual(output.await_count, 1)
                progress.assert_called_with(
                    "需要确认处理范围" if blocked else "已根据对话内容整理回复"
                )

    def test_disallowed_target_or_untracked_evidence_stops_before_write(self):
        (self.repo / "draft.txt").write_text("untracked")
        for name, path in (
            ("outside", "README.md"),
            ("alpha", "../README.md"),
            ("alpha", "draft.txt"),
        ):
            with self.subTest(name=name, path=path):
                with self.assertRaises(BridgeError):
                    self.run_with_decision(
                        {
                            "mode": "modify",
                            "repository": name,
                            "evidence_file": path,
                            "reason": "证据。",
                        }
                    )

    def test_posted_legacy_approval_does_not_gain_automatic_write_scope(self):
        legacy = dict(self.request)
        del legacy["auto_route"]
        with (
            patch(
                "chat_agent_gateway.backends.codex.locate_repository", new=AsyncMock()
            ) as route,
            patch(
                "chat_agent_gateway.backends.codex.codex_output",
                new=AsyncMock(return_value="分析完成。"),
            ) as output,
        ):
            self.assertEqual(asyncio.run(run_codex(legacy)), ("分析完成。", False))
        route.assert_not_awaited()
        self.assertEqual(output.await_args.args[2], "read-only")

    def test_unavailable_worktree_is_reported_and_never_substituted(self):
        def worktree(name):
            if name == "missing":
                raise BridgeError("missing 的指定工作树未签出。")
            return self.repo

        decision = json.dumps(
            {
                "mode": "inspect",
                "repository": "alpha",
                "evidence_file": "README.md",
                "reason": "源文件",
            }
        )
        with (
            patch(
                "chat_agent_gateway.backends.codex.available_repositories",
                return_value=["missing", "alpha"],
            ),
            patch(
                "chat_agent_gateway.backends.codex.workspace_for", side_effect=worktree
            ),
            patch(
                "chat_agent_gateway.backends.codex.codex_output",
                new=AsyncMock(side_effect=[decision, "完成", decision]),
            ) as output,
        ):
            self.assertEqual(
                asyncio.run(run_codex({**self.request, "summary": "检查 alpha"})),
                ("完成", False),
            )
            self.assertIn(
                "missing 的指定工作树未签出", output.await_args_list[0].args[0]
            )
            self.assertEqual(
                output.await_args_list[1].args[1:], (self.repo, "read-only")
            )
            with self.assertRaisesRegex(BridgeError, "指定工作树未签出"):
                asyncio.run(
                    run_codex({**self.request, "summary": "修复 missing 的配置"})
                )
            self.assertEqual(output.await_count, 3)


class ReloadTests(unittest.TestCase):
    def test_reload_waits_for_active_work_before_stopping_workers(self):
        checked = Event()

        def preflight():
            checked.set()
            return True

        async def scenario():
            work_gate = asyncio.Lock()
            delivery_gate = asyncio.Lock()
            stopping = asyncio.Event()
            async with work_gate, delivery_gate:
                reload_task = asyncio.create_task(
                    watch_code("old", work_gate, delivery_gate, stopping)
                )
                self.assertTrue(await asyncio.to_thread(checked.wait, 1))
                self.assertFalse(reload_task.done())
                self.assertFalse(stopping.is_set())
            with self.assertRaises(ReloadRequested):
                await asyncio.wait_for(reload_task, timeout=1)
            self.assertTrue(stopping.is_set())

        with (
            patch(
                "chat_agent_gateway.channels.mattermost.code_revision",
                return_value="new",
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.reload_check",
                side_effect=preflight,
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.asyncio.sleep", new=AsyncMock()
            ),
        ):
            asyncio.run(scenario())

    def test_invalid_new_code_does_not_trigger_reload(self):
        async def scenario():
            stopping = asyncio.Event()
            with self.assertRaises(asyncio.CancelledError):
                await watch_code("old", asyncio.Lock(), asyncio.Lock(), stopping)
            self.assertFalse(stopping.is_set())

        with (
            patch(
                "chat_agent_gateway.channels.mattermost.code_revision",
                return_value="new",
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.reload_check",
                return_value=False,
            ) as check,
            patch(
                "chat_agent_gateway.channels.mattermost.asyncio.sleep",
                new=AsyncMock(side_effect=[None, None, asyncio.CancelledError]),
            ),
            patch("builtins.print") as output,
        ):
            asyncio.run(scenario())
        check.assert_called_once()
        self.assertIn("导入检查没通过", output.call_args.args[0])

    @unittest.skipUnless(os.name == "posix", "Descriptor-preserving exec is POSIX-only")
    def test_exec_transfers_session_and_keeps_instance_lock(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            probe = state / "probe.py"
            probe.write_text(
                f"import sys; sys.path.insert(0, {str(root)!r})\n"
                "import fcntl, os\n"
                "from pathlib import Path\n"
                "from chat_agent_gateway.channels.mattermost import read_reload_session\n"
                "assert 'fixture-session' not in ' '.join(sys.argv)\n"
                "assert sys.argv[1:5] == ['--backend', 'fixture-backend', 'mattermost', 'run']\n"
                "auth, fd = read_reload_session(int(sys.argv[-1]))\n"
                "assert auth == 'Bearer fixture-session'\n"
                "lock = Path(sys.argv[sys.argv.index('--state-dir') + 1]) / 'lock'\n"
                "assert os.path.samestat(os.fstat(fd), lock.stat())\n"
                "with lock.open() as competing_lock:\n"
                "    try: fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "    except BlockingIOError: print('session and lock transferred')\n"
                "    else: raise AssertionError('second instance acquired the lock')\n"
            )
            setup = (
                "import fcntl, sys\n"
                "from pathlib import Path\n"
                "from types import SimpleNamespace\n"
                f"sys.path.insert(0, {str(root)!r})\n"
                "from chat_agent_gateway.channels import mattermost as pilot\n"
                f"pilot.ENTRY_MODULE = {probe.stem!r}\n"
                f"state = Path({str(state)!r})\n"
                "with (state / 'lock').open('a') as lock:\n"
                "    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "    pilot.restart_with_session(SimpleNamespace(authorization='Bearer fixture-session'), lock, state, backend='fixture-backend')\n"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", setup],
                cwd=state,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.stdout.strip(), "session and lock transferred")


class MattermostLoginTests(unittest.TestCase):
    def test_login_uses_password_and_mfa_once_and_captures_actual_expiry(self):
        headers = Message()
        headers["Token"] = "fixture-session"
        headers["Set-Cookie"] = (
            "MMUSERID=fixture; Expires=Sat, 19 Dec 2026 08:00:00 GMT"
        )
        headers["Set-Cookie"] = (
            "MMAUTHTOKEN=fixture-session; Expires=Sun, 20 Dec 2026 08:00:00 GMT; Secure; HttpOnly"
        )
        anonymous = Mock()
        anonymous.request_with_headers.return_value = {}, headers
        authenticated = Mock()
        with (
            patch(
                "chat_agent_gateway.channels.mattermost.sys.stdin.isatty",
                return_value=True,
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.getpass",
                side_effect=("fixture-password", "123456"),
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.Api",
                side_effect=(anonymous, authenticated),
            ) as api,
        ):
            client, expires = sign_in_mattermost()
        self.assertIs(client, authenticated)
        self.assertEqual(expires.timestamp(), 1797753600)
        self.assertEqual(
            api.call_args_list,
            [call(MATTERMOST, None), call(MATTERMOST, "Bearer fixture-session")],
        )
        anonymous.request_with_headers.assert_called_once_with(
            "/api/v4/users/login",
            {
                "login_id": RECIPIENT_EMAIL,
                "password": "fixture-password",
                "token": "123456",
            },
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
        )

    def test_invalid_terminal_and_invalid_mfa_never_send_credentials(self):
        with (
            patch(
                "chat_agent_gateway.channels.mattermost.sys.stdin.isatty",
                return_value=False,
            ),
            patch("chat_agent_gateway.channels.mattermost.Api") as api,
        ):
            with self.assertRaisesRegex(BridgeError, "在终端"):
                sign_in_mattermost()
            api.assert_not_called()
        with (
            patch(
                "chat_agent_gateway.channels.mattermost.sys.stdin.isatty",
                return_value=True,
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.getpass",
                side_effect=("fixture-password", "123"),
            ),
            patch("chat_agent_gateway.channels.mattermost.Api") as api,
        ):
            with self.assertRaisesRegex(BridgeError, "6 位验证码"):
                sign_in_mattermost()
            api.assert_not_called()

    def test_failed_login_has_no_automatic_retry(self):
        with (
            patch(
                "chat_agent_gateway.channels.mattermost.sys.stdin.isatty",
                return_value=True,
            ),
            patch(
                "chat_agent_gateway.channels.mattermost.getpass",
                side_effect=("fixture-password", "123456"),
            ),
            patch("chat_agent_gateway.channels.mattermost.Api") as api,
        ):
            login = api.return_value.request_with_headers
            login.side_effect = ApiError(MATTERMOST, 401)
            with self.assertRaisesRegex(BridgeError, "核对密码和当前验证码"):
                sign_in_mattermost()
            self.assertEqual(login.call_count, 1)

    def test_session_validates_current_user_and_logs_out_when_finished(self):
        client = Mock()
        user = {"id": ME, "username": "operator", "email": RECIPIENT_EMAIL}
        client.request.side_effect = [user, {}]
        with (
            patch(
                "chat_agent_gateway.channels.mattermost.sign_in_mattermost",
                return_value=(client, None),
            ),
            patch("builtins.print"),
        ):
            with mattermost_session() as (session, account):
                self.assertIs(session, client)
                self.assertEqual(account, user)
        self.assertEqual(
            client.request.call_args_list,
            [call("/api/v4/users/me"), call("/api/v4/users/logout", {})],
        )

    def test_reloaded_session_uses_existing_login_without_asking_again(self):
        client = Mock()
        user = {"id": ME, "username": "operator", "email": RECIPIENT_EMAIL}
        client.request.side_effect = [user, {}]
        with (
            patch("chat_agent_gateway.channels.mattermost.sign_in_mattermost") as login,
            patch(
                "chat_agent_gateway.channels.mattermost.Api", return_value=client
            ) as api,
            patch("builtins.print"),
        ):
            with mattermost_session("Bearer fixture-session") as (session, account):
                self.assertIs(session, client)
                self.assertEqual(account, user)
        login.assert_not_called()
        api.assert_called_once_with(MATTERMOST, "Bearer fixture-session")


if __name__ == "__main__":
    unittest.main()
